"""Build/deploy-time provider CLI version resolution.

``runtime/packages.txt`` pins the harness CLI version by hand. This module
adds the "latest stable" lane: ``opencode_version`` — or its
``SBX_OPENCODE_VERSION`` env override — may hold ``latest``, which is
resolved **once** on the build host at image build time and then frozen:

- ``opencode`` → ``opencode-ai``: the registry ``latest`` dist-tag
  (``{SBX_NPM_REGISTRY}/<pkg>/latest``).

The frozen set is written to a lock file — ``runtime/versions.lock.json`` —
as the build's version evidence and its reproducibility recipe: passing the
lock back via ``SBX_VERSIONS_LOCK`` replays those exact versions with no
upstream calls, so a previous build reproduces byte-for-byte.

Nothing here runs at sandbox start: sandboxes only ever see the pinned
image, so no floating ``@latest`` install ever happens per-Sandbox.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from runtime.image import (
    PackageSpec,
    load_packages,
)

RUNTIME_DIR = Path(__file__).resolve().parent

LATEST = "latest"
LOCK_ENV = "SBX_VERSIONS_LOCK"
LOCK_OUT_ENV = "SBX_VERSIONS_LOCK_OUT"
NPM_REGISTRY_ENV = "SBX_NPM_REGISTRY"
DEFAULT_NPM_REGISTRY = "https://registry.npmjs.org"
DEFAULT_LOCK_PATH = RUNTIME_DIR / "versions.lock.json"
LOCK_SCHEMA = "sbx-runtime/cli-versions@1"
FETCH_TIMEOUT_S = 15.0

# Explicit per-provider version overrides (env wins over packages.txt). Each
# accepts a concrete version or ``latest``.
VERSION_ENVS: dict[str, str] = {
    "opencode": "SBX_OPENCODE_VERSION",
}

_SPEC_FIELDS: dict[str, str] = {
    "opencode": "opencode_version",
}
_NPM_FIELDS: dict[str, str] = {
    "opencode": "opencode_npm",
}
# All providers the resolver knows about (the unified harness set).
PROVIDERS: tuple[str, ...] = tuple(_SPEC_FIELDS)

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")


class VersionResolutionError(Exception):
    """One provider's ``latest``/override could not be resolved."""

    def __init__(self, provider: str, detail: str, *, hint: str = "") -> None:
        self.provider = provider
        self.detail = detail
        self.hint = hint
        super().__init__(f"{provider}: {detail}" + (f" hint: {hint}" if hint else ""))


@dataclass(frozen=True)
class VersionEntry:
    """Resolution evidence for one provider CLI."""

    provider: str
    requested: str  # the packages.txt/env request, e.g. "latest" or "1.18.29"
    version: str | None  # concrete resolved version; None when unresolved
    # pin | env-override | npm-dist-tag | lock | unresolved
    source: str
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class ResolvedVersions:
    """Concrete ``PackageSpec`` + per-provider resolution evidence.

    ``spec`` carries the concrete resolved values for the selected providers;
    unselected providers keep their raw packages.txt values. ``entries``
    covers only the selected providers — the set a build actually froze.
    """

    spec: PackageSpec
    entries: Mapping[str, VersionEntry]
    resolved_at: str
    replayed_from: str | None

    def cli_versions(self) -> dict[str, str]:
        """provider → concrete version for the resolved set."""
        return {p: e.version for p, e in self.entries.items() if e.version}

    def lock_payload(self) -> dict[str, Any]:
        """Freeze/evidence document written to the lock file."""
        return {
            "schema": LOCK_SCHEMA,
            "resolved_at": self.resolved_at,
            "replayed_from": self.replayed_from,
            "providers": {
                p: {
                    "requested": e.requested,
                    "version": e.version,
                    "source": e.source,
                    "evidence": dict(e.evidence),
                }
                for p, e in sorted(self.entries.items())
            },
        }


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _fetch_json(url: str) -> Mapping[str, Any]:
    """GET ``url`` as JSON (build-host probe only — never inside an image)."""
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise VersionResolutionError("", f"GET {url} returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise VersionResolutionError("", f"GET {url} failed: {exc}") from exc


def _request_for(provider: str, spec: PackageSpec, env: Mapping[str, str]) -> tuple[str, str]:
    """Effective version request for a provider: env override > packages.txt."""
    env_name = VERSION_ENVS[provider]
    override = (env.get(env_name) or "").strip()
    if override:
        return override, "env"
    return str(getattr(spec, _SPEC_FIELDS[provider])), "packages.txt"


def _resolve_npm_latest(
    provider: str, spec: PackageSpec, env: Mapping[str, str], fetch: Callable
) -> VersionEntry:
    package = str(getattr(spec, _NPM_FIELDS[provider]))
    registry = (env.get(NPM_REGISTRY_ENV) or DEFAULT_NPM_REGISTRY).rstrip("/")
    url = f"{registry}/{quote(package, safe='')}/latest"
    try:
        data = fetch(url)
    except VersionResolutionError as exc:
        raise VersionResolutionError(
            provider,
            exc.detail,
            hint=f"check npm registry access ({registry}) or pin "
            f"{_SPEC_FIELDS[provider]} in runtime/packages.txt",
        ) from exc
    version = str(data.get("version") or "").strip()
    if not version:
        raise VersionResolutionError(
            provider, f"npm latest document for {package} has no version field"
        )
    return VersionEntry(
        provider=provider,
        requested=LATEST,
        version=version,
        source="npm-dist-tag",
        evidence={"package": package, "registry": registry, "dist_tag": LATEST, "url": url},
    )


def read_lock(path: Path) -> Mapping[str, Any] | None:
    """Read a frozen ``versions.lock.json``; ``None`` when absent/invalid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, Mapping) or data.get("schema") != LOCK_SCHEMA:
        return None
    return data


def write_lock(resolved: ResolvedVersions, path: Path | None = None) -> Path:
    """Freeze the resolved set to disk — the build's version evidence."""
    dest = path or DEFAULT_LOCK_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(resolved.lock_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return dest


def lock_path_for(env: Mapping[str, str]) -> Path | None:
    """Replay source: ``SBX_VERSIONS_LOCK`` pointing at a frozen lock file."""
    raw = (env.get(LOCK_ENV) or "").strip()
    return Path(raw) if raw else None


def lock_out_path_for(env: Mapping[str, str]) -> Path:
    """Freeze destination: ``SBX_VERSIONS_LOCK_OUT`` else the repo default."""
    raw = (env.get(LOCK_OUT_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_LOCK_PATH


def resolve_versions(
    spec: PackageSpec | None = None,
    env: Mapping[str, str] | None = None,
    *,
    providers: set[str] | frozenset[str] | None = None,
    fetch: Callable[[str], Mapping[str, Any]] | None = None,
    now: Callable[[], str] | None = None,
    lock: Path | None = None,
    offline: bool = False,
) -> ResolvedVersions:
    """Resolve the effective CLI versions for a build, once.

    ``providers`` selects which providers resolve — a build freezes only
    what it installs. ``lock``/``SBX_VERSIONS_LOCK`` replays a frozen set
    verbatim — the rollback/reproducibility lane: a locked provider's frozen
    version wins over both ``latest`` and pins, and no upstream call is made
    for it. An explicitly supplied lock path that is missing or invalid
    raises ``VersionResolutionError`` — a replay request never degrades
    silently into a fresh ``latest`` resolve. ``offline`` resolves
    ``latest`` only from a lock (env-provided or the default lock file) —
    used by pure evidence paths like ``--manifest``.

    ``fetch``/``now`` exist so tests never touch the network.
    """
    spec = spec or load_packages()
    env = os.environ if env is None else env
    fetch = fetch or _fetch_json
    selected = frozenset(providers) if providers is not None else frozenset(PROVIDERS)

    env_lock = lock_path_for(env)
    explicit_replay = lock is not None or env_lock is not None
    candidates: list[Path] = []
    if lock is not None:
        candidates.append(lock)
    if env_lock is not None:
        candidates.append(env_lock)
    if not candidates and offline:
        candidates.append(lock_out_path_for(env))
    lock_data: Mapping[str, Any] | None = None
    replayed_from: str | None = None
    for candidate in candidates:
        lock_data = read_lock(candidate)
        if lock_data is not None:
            replayed_from = str(candidate)
            break
        if explicit_replay:
            raise VersionResolutionError(
                "",
                f"versions lock {candidate} is missing or invalid",
                hint=f"the file must exist and carry the {LOCK_SCHEMA} "
                "schema (a lock written by a previous build); fix "
                "the path or drop SBX_VERSIONS_LOCK to "
                "resolve fresh versions",
            )
    locked: Mapping[str, Any] = {}
    if isinstance(lock_data, Mapping) and isinstance(lock_data.get("providers"), Mapping):
        locked = lock_data["providers"]

    entries: dict[str, VersionEntry] = {}
    overrides: dict[str, Any] = {}
    for provider in sorted(selected):
        if provider not in _SPEC_FIELDS:
            raise VersionResolutionError(provider, f"unknown provider {provider!r}")
        request, origin = _request_for(provider, spec, env)
        field = _SPEC_FIELDS[provider]

        locked_entry = locked.get(provider)
        locked_version = ""
        if isinstance(locked_entry, Mapping):
            locked_version = str(locked_entry.get("version") or "").strip()

        if explicit_replay and locked_version:
            # Replay lane: the frozen version wins over latest and pins.
            entries[provider] = VersionEntry(
                provider=provider,
                requested=request,
                version=locked_version,
                source="lock",
                evidence={"lock": replayed_from},
            )
            overrides[field] = locked_version
            continue

        if request == LATEST and offline:
            if locked_version:
                entries[provider] = VersionEntry(
                    provider=provider,
                    requested=request,
                    version=locked_version,
                    source="lock",
                    evidence={"lock": replayed_from},
                )
                overrides[field] = locked_version
            else:
                entries[provider] = VersionEntry(
                    provider=provider,
                    requested=request,
                    version=None,
                    source="unresolved",
                    evidence={},
                )
            continue

        if request == LATEST:
            entry = _resolve_npm_latest(provider, spec, env, fetch)
        else:
            entry = VersionEntry(
                provider=provider,
                requested=request,
                version=request,
                source="env-override" if origin == "env" else "pin",
                evidence={"env": VERSION_ENVS[provider]} if origin == "env" else {},
            )
        if entry.version is None:
            raise VersionResolutionError(provider, "resolution returned no version")
        entries[provider] = entry
        overrides[field] = entry.version

    return ResolvedVersions(
        spec=replace(spec, **overrides),
        entries=entries,
        resolved_at=(now or _iso_now)(),
        replayed_from=replayed_from,
    )

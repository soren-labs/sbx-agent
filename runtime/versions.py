"""Build/deploy-time provider CLI version resolution (SOR-175).

``runtime/packages.txt`` pins provider CLI versions by hand. This module adds
the "latest stable" lane: any ``*_version`` key — or its ``SBX_*_VERSION`` env
override — may hold ``latest``, which is resolved **once** on the build host at
image build / deploy time and then frozen for that deployment:

- npm providers (``codex`` → ``@openai/codex``, ``opencode`` →
  ``opencode-ai``): the registry ``latest`` dist-tag
  (``{SBX_NPM_REGISTRY}/<pkg>/latest``).
- ``devin``: ``{devin_base_url}/current/manifest.json`` — the promoted
  release pointer the official installer uses — which also carries the
  per-platform sha256 checksums ``install-devin.sh`` verifies. An env-pinned
  Devin version resolves checksums from ``{base}/<ver>/manifest.json`` unless
  ``SBX_DEVIN_SHA256_X86_64`` / ``SBX_DEVIN_SHA256_AARCH64`` are set.
- ``antigravity`` / ``grok``: the build-host binary's own ``--version``
  (these CLIs are host artifacts; "latest" means whatever the host has).

The frozen set is written to a lock file — ``runtime/versions.lock.json`` for
standalone image builds / ``--resolve-versions``, ``<state>/cli-versions.json``
under ``sbx deploy`` — as the deployment's version evidence and its rollback
recipe: passing the lock back via ``SBX_VERSIONS_LOCK`` (or ``sbx deploy
--versions-lock``) replays those exact versions with no upstream calls, so a
previous deployment reproduces byte-for-byte.

Nothing here runs at Sandbox start: sandboxes only ever see the pinned image,
so no floating ``@latest`` install ever happens per-Sandbox.
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
    AGY_BIN_ENV,
    DEFAULT_AGY_BIN,
    DEFAULT_GROK_BIN,
    GROK_BIN_ENV,
    PackageSpec,
    _cli_version_output,
    _host_cli_bin,
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
    "codex": "SBX_CODEX_VERSION",
    "devin": "SBX_DEVIN_VERSION",
    "opencode": "SBX_OPENCODE_VERSION",
    "antigravity": "SBX_AGY_VERSION",
    "grok": "SBX_GROK_VERSION",
}
DEVIN_SHA256_ENVS: dict[str, str] = {
    "x86_64-unknown-linux": "SBX_DEVIN_SHA256_X86_64",
    "aarch64-unknown-linux": "SBX_DEVIN_SHA256_AARCH64",
}
# Devin bundle targets the image needs checksums for.
_DEVIN_TARGETS = ("x86_64-unknown-linux", "aarch64-unknown-linux")

_SPEC_FIELDS: dict[str, str] = {
    "codex": "codex_version",
    "devin": "devin_version",
    "opencode": "opencode_version",
    "antigravity": "agy_version",
    "grok": "grok_version",
}
_NPM_FIELDS: dict[str, str] = {
    "codex": "codex_npm",
    "opencode": "opencode_npm",
}
_HOST_BINS: dict[str, tuple[str, Path, str]] = {
    "antigravity": (AGY_BIN_ENV, DEFAULT_AGY_BIN, "agy"),
    "grok": (GROK_BIN_ENV, DEFAULT_GROK_BIN, "grok"),
}
# All providers the resolver knows about (the contract provider set).
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
    requested: str  # the packages.txt/env request, e.g. "latest" or "0.153.0"
    version: str | None  # concrete resolved version; None when unresolved
    # pin | env-override | npm-dist-tag | devin-manifest | host-binary | lock | unresolved
    source: str
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class ResolvedVersions:
    """Concrete ``PackageSpec`` + per-provider resolution evidence.

    ``spec`` carries the concrete resolved values for the selected providers;
    unselected providers keep their raw packages.txt values. ``entries``
    covers only the selected providers — the set a deployment actually froze.
    """

    spec: PackageSpec
    entries: Mapping[str, VersionEntry]
    resolved_at: str
    replayed_from: str | None

    def cli_versions(self) -> dict[str, str]:
        """provider → concrete version for the resolved set."""
        return {p: e.version for p, e in self.entries.items() if e.version}

    def lock_payload(self) -> dict[str, Any]:
        """Freeze/evidence document written to the deployment's lock file."""
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


def _probe_host_version(provider: str, env: Mapping[str, str]) -> tuple[str, str]:
    """Resolve ``latest`` for a host-binary provider via its ``--version``.

    Returns ``(version, bin_path)``. The build-host binary IS the resolution
    input for agy/grok — "latest" means whatever this host installs.
    """
    env_var, default, cli = _HOST_BINS[provider]
    try:
        host = _host_cli_bin(env_var, default, cli)
        out = _cli_version_output(host)
    except SystemExit as exc:
        raise VersionResolutionError(
            provider,
            str(exc),
            hint=f"install the {cli} CLI on the build host or pin {provider} "
            "in runtime/packages.txt",
        ) from exc
    match = _VERSION_RE.search(out)
    if not match:
        raise VersionResolutionError(
            provider,
            f"{cli} --version output has no version token: {out[:120]!r}",
            hint="pin an explicit version in runtime/packages.txt instead of 'latest'",
        )
    return match.group(0), str(host)


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


def _devin_manifest_url(spec: PackageSpec, env: Mapping[str, str], version: str | None) -> str:
    """Manifest URL for ``version`` (``None`` → the promoted ``current``)."""
    base = (env.get("SBX_DEVIN_BASE_URL") or spec.devin_base_url).rstrip("/")
    return f"{base}/{version or 'current'}/manifest.json"


def _devin_shas_from_manifest(data: Mapping[str, Any], url: str) -> dict[str, str]:
    platforms = data.get("platforms")
    shas: dict[str, str] = {}
    if isinstance(platforms, Mapping):
        for target in _DEVIN_TARGETS:
            entry = platforms.get(target)
            if isinstance(entry, Mapping) and entry.get("sha256"):
                shas[target] = str(entry["sha256"])
    missing = [t for t in _DEVIN_TARGETS if t not in shas]
    if missing:
        raise VersionResolutionError(
            "devin",
            f"manifest {url} is missing sha256 for {missing}",
            hint="set SBX_DEVIN_SHA256_X86_64 / SBX_DEVIN_SHA256_AARCH64 explicitly",
        )
    return shas


def _resolve_devin(
    request: str, origin: str, spec: PackageSpec, env: Mapping[str, str], fetch: Callable
) -> tuple[VersionEntry, dict[str, str]]:
    """Devin version + checksums; returns ``(entry, sha256_by_target)``.

    ``latest`` → ``current/manifest.json``. An env-pinned version → its
    versioned manifest unless ``SBX_DEVIN_SHA256_*`` are set. A packages.txt
    pin reuses the committed checksums — no fetch, fully reproducible.
    """
    if request == LATEST:
        url = _devin_manifest_url(spec, env, None)
        try:
            data = fetch(url)
        except VersionResolutionError as exc:
            raise VersionResolutionError(
                "devin",
                exc.detail,
                hint="check the Devin bundle host reachability or pin devin_version "
                "in runtime/packages.txt",
            ) from exc
        version = str(data.get("version") or "").strip()
        if not version:
            raise VersionResolutionError("devin", f"manifest {url} has no version field")
        shas = _devin_shas_from_manifest(data, url)
        return (
            VersionEntry(
                provider="devin",
                requested=LATEST,
                version=version,
                source="devin-manifest",
                evidence={"manifest_url": url, "sha256": shas},
            ),
            shas,
        )

    if origin == "packages.txt":
        return (
            VersionEntry(
                provider="devin",
                requested=request,
                version=request,
                source="pin",
                evidence={
                    "sha256": {
                        "x86_64-unknown-linux": spec.devin_sha256_x86_64,
                        "aarch64-unknown-linux": spec.devin_sha256_aarch64,
                    }
                },
            ),
            {
                "x86_64-unknown-linux": spec.devin_sha256_x86_64,
                "aarch64-unknown-linux": spec.devin_sha256_aarch64,
            },
        )

    # Env-pinned Devin version: explicit env checksums win; otherwise fetch
    # the versioned manifest so the checksum gate still runs.
    env_shas = {
        target: (env.get(env_name) or "").strip() for target, env_name in DEVIN_SHA256_ENVS.items()
    }
    if all(env_shas.values()):
        return (
            VersionEntry(
                provider="devin",
                requested=request,
                version=request,
                source="env-override",
                evidence={"sha256": dict(env_shas)},
            ),
            dict(env_shas),
        )
    if any(env_shas.values()):
        raise VersionResolutionError(
            "devin",
            "partial SBX_DEVIN_SHA256_* override",
            hint="set both SBX_DEVIN_SHA256_X86_64 and SBX_DEVIN_SHA256_AARCH64, or neither",
        )
    url = _devin_manifest_url(spec, env, request)
    try:
        data = fetch(url)
    except VersionResolutionError as exc:
        raise VersionResolutionError(
            "devin",
            exc.detail,
            hint=f"check {url} reachability, or set SBX_DEVIN_SHA256_X86_64 / "
            "SBX_DEVIN_SHA256_AARCH64 explicitly",
        ) from exc
    shas = _devin_shas_from_manifest(data, url)
    return (
        VersionEntry(
            provider="devin",
            requested=request,
            version=request,
            source="devin-manifest",
            evidence={"manifest_url": url, "sha256": shas},
        ),
        shas,
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
    """Freeze the resolved set to disk — the deployment's version evidence."""
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


def _locked_shas(
    provider: str,
    locked_entry: Mapping[str, Any],
    version: str,
    spec: PackageSpec,
    env: Mapping[str, str],
    fetch: Callable,
) -> dict[str, str]:
    """Devin checksums on lock replay: the frozen evidence, else refetched."""
    evidence = locked_entry.get("evidence")
    shas = evidence.get("sha256") if isinstance(evidence, Mapping) else None
    if isinstance(shas, Mapping) and all(shas.get(t) for t in _DEVIN_TARGETS):
        return {t: str(shas[t]) for t in _DEVIN_TARGETS}
    url = _devin_manifest_url(spec, env, version)
    try:
        data = fetch(url)
    except VersionResolutionError as exc:
        raise VersionResolutionError(
            provider,
            exc.detail,
            hint="the lock file lacks Devin checksums and they could not be "
            "refetched — keep the lock's evidence.sha256 block intact",
        ) from exc
    return _devin_shas_from_manifest(data, url)


def resolve_versions(
    spec: PackageSpec | None = None,
    env: Mapping[str, str] | None = None,
    *,
    providers: set[str] | frozenset[str] | None = None,
    fetch: Callable[[str], Mapping[str, Any]] | None = None,
    host_probe: Callable[[str, Mapping[str, str]], tuple[str, str]] | None = None,
    now: Callable[[], str] | None = None,
    lock: Path | None = None,
    offline: bool = False,
) -> ResolvedVersions:
    """Resolve the effective CLI versions for a build/deploy, once.

    ``providers`` selects which providers resolve — a deployment freezes only
    what it builds (deploy passes the enabled set plus ``codex``, which rides
    in every image). ``lock``/``SBX_VERSIONS_LOCK`` replays a frozen set
    verbatim — the rollback/reproducibility lane: a locked provider's frozen
    version wins over both ``latest`` and pins, and no upstream call is made
    for it. An explicitly supplied lock path that is missing or invalid
    raises ``VersionResolutionError`` — a replay request never degrades
    silently into a fresh ``latest`` resolve. ``offline`` resolves
    ``latest`` only from a lock (env-provided or the default lock file) —
    used by pure evidence paths like ``--manifest``.

    ``fetch``/``host_probe``/``now`` exist so tests never touch the network
    or real provider binaries.
    """
    spec = spec or load_packages()
    env = os.environ if env is None else env
    fetch = fetch or _fetch_json
    host_probe = host_probe or _probe_host_version
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
                "schema (a lock written by a previous build/deploy); fix "
                "the path or drop --versions-lock / SBX_VERSIONS_LOCK to "
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
            if provider == "devin":
                shas = _locked_shas(provider, locked_entry, locked_version, spec, env, fetch)
                overrides["devin_sha256_x86_64"] = shas["x86_64-unknown-linux"]
                overrides["devin_sha256_aarch64"] = shas["aarch64-unknown-linux"]
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
            if provider in _NPM_FIELDS:
                entry = _resolve_npm_latest(provider, spec, env, fetch)
            elif provider == "devin":
                entry, shas = _resolve_devin(request, origin, spec, env, fetch)
                overrides["devin_sha256_x86_64"] = shas["x86_64-unknown-linux"]
                overrides["devin_sha256_aarch64"] = shas["aarch64-unknown-linux"]
            else:
                version, host = host_probe(provider, env)
                entry = VersionEntry(
                    provider=provider,
                    requested=request,
                    version=version,
                    source="host-binary",
                    evidence={"bin": host, "env": _HOST_BINS[provider][0]},
                )
        elif provider == "devin":
            entry, shas = _resolve_devin(request, origin, spec, env, fetch)
            overrides["devin_sha256_x86_64"] = shas["x86_64-unknown-linux"]
            overrides["devin_sha256_aarch64"] = shas["aarch64-unknown-linux"]
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

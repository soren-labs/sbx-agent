"""SOR-204 provider/account capability discovery — models + effort levels.

Replaces static ``default_models`` as the source of truth for what a
provider *account* can actually run. Each provider CLI answers a ``models``
argv (``control.onboarding.PROVIDER_MODEL_CHECKS``) inside a throwaway
sandbox — same shape as ``SandboxAuthVerifyProbe``: ``runner init``
restores the credential, the models listing runs with the credential env
scrubbed, and the output is parsed tolerantly (JSON first, then plain
tables/lists) into ``ModelCapability`` rows carrying canonical
``reasoning_efforts`` plus the provider-native token map.

``CapabilityCatalog`` is a TTL cache (``SBX_CAPABILITY_TTL_S``, default
300 s) with stale-last-good semantics: a failed refresh keeps serving the
last successful snapshot marked ``stale``, and a cold account falls back
to declared models (``account.models`` → ``SBX_<PROVIDER>_MODELS`` → the
provider's built-in defaults) so the API never blocks on discovery.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from runtime.runner.effort import (
    CANONICAL_EFFORTS,
    canonical_effort,
    is_effort_token,
    native_effort,
    split_effort_suffix,
    supported_efforts,
)

from control.api_v1.bootstrap import PROVIDER_DEFAULT_MODELS
from control.onboarding import (
    ACCOUNT_ID_ENV,
    CREDENTIAL_ENV,
    classify_auth_output,
    output_has_auth_failure,
    provider_auth_argv,
    provider_cli_env,
    provider_models_argv,
)
from control.ports import Account

_DISCOVERY_PURPOSE = "capability-probe"

# Providers whose effort is an orthogonal CLI flag/config knob — the
# verified ``SUPPORTED_EFFORTS`` floor may fill in when a listing omits
# per-model efforts. Providers absent here (devin, antigravity, opencode)
# encode the tier in the model id or have no effort surface, so a bare
# listing row must not inherit a floor.
_FLAG_EFFORT_PROVIDERS = frozenset({"codex", "grok"})


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _env_ttl() -> float:
    try:
        return float(os.environ.get("SBX_CAPABILITY_TTL_S", "300"))
    except ValueError:
        return 300.0


# ------------------------------------------------------------------ rows


@dataclass(frozen=True)
class ModelCapability:
    """One servable model on a provider account."""

    model: str
    display_name: str
    family: str
    aliases: tuple[str, ...]
    reasoning_efforts: tuple[str, ...]  # canonical levels, ordered
    effort_native: dict[str, str]  # canonical -> provider-native token
    default_effort: str | None  # canonical


@dataclass(frozen=True)
class CapabilitySnapshot:
    """What the catalog currently believes about one account.

    ``source``: ``discovered`` (live CLI probe), ``declared``
    (``account.models``), ``env`` (``SBX_<PROVIDER>_MODELS``) or ``static``
    (built-in defaults). ``stale`` means the last refresh failed or the
    snapshot outlived the TTL — the served data is last-good.
    """

    provider: str
    account_id: str
    models: tuple[ModelCapability, ...]
    source: str
    refreshed_at: str | None
    stale: bool
    error: str | None = None
    default_model: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    models: tuple[ModelCapability, ...] = ()
    default_model: str | None = None
    source: str = "discovered"
    error: str | None = None


# ------------------------------------------------------------- fallbacks


def fallback_models(provider: str, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """``SBX_<PROVIDER>_MODELS`` override, else the built-in defaults."""
    env = os.environ if env is None else env
    raw = env.get(f"SBX_{provider.upper()}_MODELS")
    if raw:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    return PROVIDER_DEFAULT_MODELS.get(provider, ())


def declared_model_ids(
    account: Account, env: Mapping[str, str] | None = None
) -> tuple[tuple[str, ...], str]:
    """Model ids to advertise before discovery runs, and their source."""
    env = os.environ if env is None else env
    if account.models:
        return tuple(account.models), "declared"
    raw = env.get(f"SBX_{account.provider.upper()}_MODELS")
    if raw:
        return tuple(part.strip() for part in raw.split(",") if part.strip()), "env"
    return PROVIDER_DEFAULT_MODELS.get(account.provider, ()), "static"


# ----------------------------------------------------------------- parse

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_LEADING_MARK = re.compile(r"^(?:[-*•·→>]+|\d+[.)])\s+")
_MODEL_TOKEN = re.compile(r"^[A-Za-z][\w./:~-]*$")
_FLAG_MARKERS = ("(default)", "[default]", "(current)", "[current]", "*")


def infer_family(model: str) -> str:
    """Product family from a model id: ``openai/gpt-5.6-luna`` -> ``gpt``."""
    stem, _ = split_effort_suffix(model)
    tail = stem.rsplit("/", 1)[-1]
    parts: list[str] = []
    for seg in tail.split("-"):
        if seg.isalpha():
            parts.append(seg.lower())
        else:
            break
    return "-".join(parts) or tail


def _pretty_display(model: str) -> str:
    tail = model.rsplit("/", 1)[-1]
    return re.sub(r"[-_.]+", " ", tail).title()


@dataclass
class _Entry:
    model: str
    display: str | None = None
    family: str | None = None
    aliases: tuple[str, ...] = ()
    efforts: tuple[str, ...] = ()
    default: str | None = None
    is_default: bool = False


def _json_entries(output: str) -> list[_Entry] | None:
    text = output.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        for key in ("models", "data", "results", "items", "families"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    # ``devin models list --format json`` nests rows under families:
    # {"families": [{"slug", "family_label", "aliases", "variants": [
    #   {"model_uid", "label", ...}]}]} — flatten variants into entries.
    flattened: list[Any] = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and isinstance(item.get("variants"), list):
            fam = item.get("family_uid") or item.get("slug")
            fam_aliases = item.get("aliases") or ()
            for variant in item["variants"]:
                if not isinstance(variant, dict):
                    continue
                row = dict(variant)
                row.setdefault("family", fam)
                if fam_aliases and not row.get("aliases"):
                    row["aliases"] = fam_aliases
                flattened.append(row)
        else:
            flattened.append(item)
    data = flattened or data
    if not isinstance(data, list):
        return None
    entries: list[_Entry] = []
    for item in data:
        if isinstance(item, str):
            entries.append(_Entry(model=item))
            continue
        if not isinstance(item, dict):
            continue
        # Hidden/non-API catalog rows are not servable (codex debug models
        # marks them ``visibility: "hide"`` / ``supported_in_api: false``).
        if item.get("supported_in_api") is False:
            continue
        if str(item.get("visibility") or "").lower() in ("hide", "hidden"):
            continue
        model = next(
            (str(item[k]) for k in ("id", "model", "slug", "model_id", "model_uid") if item.get(k)),
            None,
        )
        if model is None and item.get("name"):
            model = str(item["name"])
        if not model:
            continue
        display = next(
            (str(item[k]) for k in ("display_name", "display", "title", "label") if item.get(k)),
            None,
        )
        if display is None and isinstance(item.get("name"), str) and item["name"] != model:
            display = item["name"]
        efforts_raw = (
            item.get("efforts")
            or item.get("reasoning_efforts")
            or item.get("effort_levels")
            or item.get("supported_reasoning_levels")
        )
        efforts: list[str] = []
        if isinstance(efforts_raw, dict):
            efforts = [str(k) for k, v in efforts_raw.items() if v]
        elif isinstance(efforts_raw, list):
            for e in efforts_raw:
                if isinstance(e, str):
                    efforts.append(e)
                elif isinstance(e, dict):
                    val = e.get("level") or e.get("effort") or e.get("id") or e.get("name")
                    if val:
                        efforts.append(str(val))
        aliases_raw = item.get("aliases") or item.get("alias") or ()
        if isinstance(aliases_raw, str):
            aliases_raw = (aliases_raw,)
        default = next(
            (
                str(item[k])
                for k in (
                    "default_effort",
                    "default_reasoning_effort",
                    "default_reasoning_level",
                    "default_level",
                )
                if item.get(k)
            ),
            None,
        )
        entries.append(
            _Entry(
                model=model,
                display=display,
                family=(
                    str(item["family"] or item["group"])
                    if item.get("family") or item.get("group")
                    else None
                ),
                aliases=tuple(str(a) for a in aliases_raw),
                efforts=tuple(efforts),
                default=default,
                is_default=bool(
                    item.get("default") is True or item.get("is_default") or item.get("selected")
                ),
            )
        )
    return entries


def _text_entries(output: str) -> list[_Entry]:
    entries: list[_Entry] = []
    for raw_line in output.splitlines():
        line = _LEADING_MARK.sub("", _ANSI.sub("", raw_line).strip())
        if not line:
            continue
        tokens = line.split()
        if not tokens:
            continue
        model = tokens[0].rstrip("*")
        if not _MODEL_TOKEN.match(model):
            continue
        # A model id is never a bare word — require a digit or provider
        # prefix (``provider/model``) to keep headers and prose out.
        if not any(ch.isdigit() for ch in model) and "/" not in model:
            continue
        rest = tokens[1:]
        is_default = any(tok.lower() in _FLAG_MARKERS for tok in tokens[1:]) or tokens[0].endswith(
            "*"
        )
        rest = [tok for tok in rest if tok.lower() not in _FLAG_MARKERS]
        efforts: list[str] = []
        display_parts: list[str] = []
        for tok in rest:
            kv = re.match(r"(?:efforts?|reasoning|levels?)[=:](.+)", tok, re.I)
            if kv:
                efforts.extend(re.split(r"[,/]", kv.group(1).strip("[]()")))
                continue
            if tok.startswith("(") or tok.startswith("["):
                inner = tok.strip("()[]")
                if all(is_effort_token("", p) for p in re.split(r"[,/]", inner) if p):
                    efforts.extend(p for p in re.split(r"[,/]", inner) if p)
                    continue
            if is_effort_token("", tok):
                efforts.append(tok)
                continue
            display_parts.append(tok)
        entries.append(
            _Entry(
                model=model,
                display=" ".join(display_parts) or None,
                efforts=tuple(efforts),
                is_default=is_default,
            )
        )
    return entries


def _capability_from(provider: str, entry: _Entry) -> ModelCapability:
    model = entry.model.strip()
    stem, suffix = split_effort_suffix(model)

    canon: list[str] = []
    effort_native: dict[str, str] = {}
    for token in entry.efforts:
        level = canonical_effort(provider, token)
        if level and level not in canon:
            canon.append(level)
            effort_native[level] = token
    if not canon and provider in _FLAG_EFFORT_PROVIDERS:
        # No explicit effort list: the verified floor applies only where
        # effort is an orthogonal CLI flag/config (codex config.toml,
        # grok --reasoning-effort). Providers that encode the tier in the
        # model id (devin ``swe-2-max``, agy ``gemini-3.8-flash-high``) must
        # not inherit it — the floor made ``claude-sonnet-4-6`` advertise
        # low/medium/high while real ``agy --effort`` runs fail
        # ``model_unavailable``.
        canon = list(supported_efforts(provider))
        effort_native = {lv: native_effort(provider, lv) for lv in canon}
    canon.sort(key=CANONICAL_EFFORTS.index)

    default_effort = canonical_effort(provider, entry.default) if entry.default else None
    aliases: list[str] = [a for a in entry.aliases if a and a != model]
    if suffix is not None:
        # ``swe-2-high`` / ``gemini-3.8-flash-low`` encode a tier in the id;
        # the stem is the capability alias, the tier the default effort.
        if stem not in aliases:
            aliases.insert(0, stem)
        default_effort = default_effort or suffix
        if provider == "antigravity" and not canon:
            # agy still accepts ``--effort`` for the tier the id encodes —
            # advertise exactly that level so the UI offers it and
            # ``agy --model gemini-3.8-flash-high --effort high`` is the
            # only combination produced. Tier-less models (claude-*) keep
            # an empty surface: ``--effort`` on them fails closed at the
            # API instead of ``model_unavailable`` mid-run.
            canon = [suffix]
            effort_native = {suffix: native_effort(provider, suffix)}
    if default_effort is not None and canon and default_effort not in canon:
        # An advertised default outside the effort surface is dropped; an
        # empty surface keeps it — the tier is baked into the model id
        # itself (``swe-2-max`` on an effort-less provider).
        default_effort = None

    return ModelCapability(
        model=model,
        display_name=entry.display or _pretty_display(model),
        family=entry.family or infer_family(model),
        aliases=tuple(aliases),
        reasoning_efforts=tuple(canon),
        effort_native=effort_native,
        default_effort=default_effort,
    )


def capability_from_model_id(provider: str, model: str) -> ModelCapability:
    """Capability derived from a bare model id — no catalog row required.

    Non-discovered snapshots permit arbitrary model ids, but the effort
    surface is still model-scoped: flag-effort providers get the verified
    floor, tier-in-id providers their suffix, and tier-less ids nothing
    (``agy --effort`` on ``claude-sonnet-4-6`` fails ``model_unavailable``).
    """
    return _capability_from(provider, _Entry(model=model))


def parse_models_output(provider: str, output: str) -> tuple[ModelCapability, ...]:
    """Best-effort parse of a provider CLI's models listing.

    Tolerates JSON (``{"models": [...]}`` / bare list) and plain-text
    tables or one-per-line lists; never raises — returns ``()`` when no
    model ids can be recognized.
    """
    entries = _json_entries(output)
    if entries is None:
        entries = _text_entries(output)
    caps: list[ModelCapability] = []
    seen: set[str] = set()
    default_first = {e.model for e in entries if e.is_default}
    for entry in entries:
        cap = _capability_from(provider, entry)
        if cap.model in seen:
            continue
        seen.add(cap.model)
        caps.append(cap)
    # A ``(default)`` marker pulls the row to the front.
    caps.sort(key=lambda c: (c.model not in default_first,))
    return tuple(caps)


def declared_snapshot(account: Account) -> CapabilitySnapshot:
    """Snapshot built from declared/env/static models — no live probe."""
    ids, source = declared_model_ids(account)
    return CapabilitySnapshot(
        provider=account.provider,
        account_id=account.id,
        models=tuple(_capability_from(account.provider, _Entry(model=m)) for m in ids),
        source=source,
        refreshed_at=None,
        stale=False,
        default_model=ids[0] if ids else None,
    )


# ------------------------------------------------------------------ probes


@runtime_checkable
class CapabilityProbe(Protocol):
    def probe(self, account: Account, blob: dict[str, Any] | None) -> DiscoveryResult:
        """Discover the account's servable models; never returns secrets."""


class DeclaredCapabilityProbe:
    """No-sandbox probe: serves declared/env/static models as ``discovered``-

    equivalent rows. Used when the plane exposes no backend (unit tests
    without a sandbox) — the catalog still gains TTL + refresh semantics.
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = env

    def probe(self, account: Account, blob: dict[str, Any] | None) -> DiscoveryResult:
        ids, source = declared_model_ids(account, self._env)
        return DiscoveryResult(
            models=tuple(_capability_from(account.provider, _Entry(model=m)) for m in ids),
            default_model=ids[0] if ids else None,
            source=source,
        )


class SandboxCapabilityProbe:
    """Throwaway-sandbox probe: ``runner init`` + the provider's ``models`` argv.

    Mirrors ``control.onboarding.SandboxAuthVerifyProbe``: create a sandbox
    tagged ``capability-probe`` with the account's Secret (or the registry
    blob via ``SBX_ACCOUNT_CREDENTIAL``), run ``runner init`` to restore the
    credential, then exec ``provider_models_argv`` with credential env
    scrubbed and parse the listing. The sandbox is always terminated.
    """

    def __init__(
        self,
        backend: Any,
        runner_cmd: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        bin_env: Mapping[str, str] | None = None,
        model: str | None = None,
    ) -> None:
        self._backend = backend
        self._runner_cmd = list(runner_cmd)
        self._env = dict(env or {})
        self._bin_env = bin_env
        self._model = model

    def probe(self, account: Account, blob: dict[str, Any] | None) -> DiscoveryResult:
        from control.backend import SandboxSpec
        from control.sandbox_io import sandbox_env

        argv = provider_models_argv(account.provider, env=self._bin_env)
        if argv is None:
            return DiscoveryResult(error=f"no models check for {account.provider!r}")
        if blob is None and not account.secret_name:
            return DiscoveryResult(error="no_credential")
        handle = None
        try:
            handle = self._backend.create(
                SandboxSpec(
                    tags={
                        "purpose": _DISCOVERY_PURPOSE,
                        "provider": account.provider,
                        "account_id": account.id,
                    },
                    secrets=[account.secret_name] if account.secret_name else [],
                    env=self._env,
                )
            )
            extra = {ACCOUNT_ID_ENV: account.id}
            if blob:
                extra[CREDENTIAL_ENV] = json.dumps(blob, ensure_ascii=False)
            env = sandbox_env(handle, extra)
            if not blob:
                env.pop(CREDENTIAL_ENV, None)
            defaults = fallback_models(account.provider)
            model = (
                self._model
                or (account.models[0] if account.models else None)
                or (defaults[0] if defaults else None)
                or "gpt-5.6-luna"
            )
            init_argv = [
                *self._runner_cmd,
                "init",
                "--auth",
                "auth_json",
                "--model",
                model,
                "--provider",
                account.provider,
                "--account-id",
                account.id,
            ]
            proc = self._backend.exec(handle, init_argv, env=env)
            for _ in proc.stdout:
                pass
            code = proc.wait()
            if code == 5:
                return DiscoveryResult(error="auth_invalid")
            if code != 0:
                return DiscoveryResult(error=f"init_failed:{code}")
            check_env = provider_cli_env(account.provider, env, Path(handle.root) / "home")
            # Auth gate: providers whose models argv is not itself an auth
            # check (codex ``debug models`` renders the bundled catalog
            # signed-out; devin ``models list`` likewise) run their own
            # auth argv first, so a dead credential can never mark the
            # static listing ``discovered``. When the auth argv *is* the
            # models argv (agy/grok), the output-marker screen below
            # covers it — ``grok models`` prints "You are not
            # authenticated." then still lists a catalog at rc 0.
            auth_argv = provider_auth_argv(account.provider, env=self._bin_env)
            if auth_argv is not None and auth_argv != argv:
                aproc = self._backend.exec(handle, auth_argv, env=check_env)
                aout = "\n".join(aproc.stdout)
                acode = aproc.wait()
                if classify_auth_output(account.provider, acode, aout) == "auth_invalid":
                    return DiscoveryResult(error="auth_invalid")
            proc = self._backend.exec(handle, argv, env=check_env)
            output = "\n".join(proc.stdout)
            code = proc.wait()
            # Marker screen only applies to text listings — JSON payloads
            # (``codex debug models`` embeds full prompt text that can
            # legitimately contain phrases like "unauthorized") are covered
            # by the auth argv pre-check above instead.
            if not output.lstrip().startswith(("[", "{")) and output_has_auth_failure(output):
                return DiscoveryResult(error="auth_invalid")
            if code != 0:
                return DiscoveryResult(error=f"models_list_failed:{code}")
            caps = parse_models_output(account.provider, output)
            if not caps:
                return DiscoveryResult(error="unparseable_models")
            return DiscoveryResult(models=caps, default_model=caps[0].model)
        except NotImplementedError:
            return DiscoveryResult(error="probe_unavailable")
        except Exception:
            return DiscoveryResult(error="probe_exec_failed")
        finally:
            if handle is not None:
                try:
                    self._backend.terminate(handle)
                except Exception:
                    pass


# ---------------------------------------------------------------- catalog


class CapabilityCatalog:
    """TTL cache of per-account ``CapabilitySnapshot``s (stale-last-good).

    ``get()`` never blocks: a fresh cached snapshot, else a stale-marked
    last-good or the declared fallback — and an expired/missing entry kicks
    a background refresh when ``auto_refresh`` is on. ``refresh()`` runs the
    probe synchronously; on failure the previous snapshot is served with
    ``stale=True`` (and a backoff timestamp so failures don't hammer).
    """

    def __init__(
        self,
        probe: CapabilityProbe,
        *,
        get_account: Callable[[str], Account | None] = lambda _id: None,
        get_blob: Callable[[str], dict[str, Any] | None] = lambda _id: None,
        ttl_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        auto_refresh: bool = True,
    ) -> None:
        self._probe = probe
        self._get_account = get_account
        self._get_blob = get_blob
        self._ttl_s = _env_ttl() if ttl_s is None else ttl_s
        self._clock = clock
        self._auto_refresh = auto_refresh
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, CapabilitySnapshot]] = {}
        self._inflight: set[str] = set()

    # -- reads

    def get(self, account: Account, *, ensure: bool = True) -> CapabilitySnapshot:
        now = self._clock()
        with self._lock:
            entry = self._cache.get(account.id)
        if entry is not None and now - entry[0] <= self._ttl_s:
            return entry[1]
        if entry is not None:
            if ensure:
                self._schedule(account.id)
            return replace(entry[1], stale=True)
        snap = self._declared(account)
        with self._lock:
            self._cache.setdefault(account.id, (now, snap))
        if ensure:
            self._schedule(account.id)
        return snap

    # -- refresh

    def refresh(self, account: Account, blob: dict[str, Any] | None = None) -> CapabilitySnapshot:
        if blob is None:
            blob = self._get_blob(account.id)
        try:
            result = self._probe.probe(account, blob)
        except Exception:
            result = DiscoveryResult(error="probe_exception")
        now = self._clock()
        if result.models:
            snap = CapabilitySnapshot(
                provider=account.provider,
                account_id=account.id,
                models=result.models,
                source=result.source,
                refreshed_at=_iso_now(),
                stale=False,
                default_model=result.default_model or result.models[0].model,
            )
        else:
            with self._lock:
                prior = self._cache.get(account.id)
            base = prior[1] if prior is not None else self._declared(account)
            snap = replace(base, stale=True, error=result.error or "no_models")
        with self._lock:
            self._cache[account.id] = (now, snap)
        return snap

    def _schedule(self, account_id: str) -> None:
        if not self._auto_refresh:
            return
        with self._lock:
            if account_id in self._inflight:
                return
            self._inflight.add(account_id)
        threading.Thread(target=self._refresh_bg, args=(account_id,), daemon=True).start()

    def _refresh_bg(self, account_id: str) -> None:
        try:
            account = self._get_account(account_id)
            if account is not None:
                self.refresh(account)
        finally:
            with self._lock:
                self._inflight.discard(account_id)

    # -- declared fallback

    def _declared(self, account: Account, error: str | None = None) -> CapabilitySnapshot:
        snap = declared_snapshot(account)
        if error is not None:
            snap = replace(snap, stale=True, error=error)
        return snap


def catalog_for_plane(plane: Any, registry: Any) -> CapabilityCatalog:
    """Production wiring: sandbox probe when the plane has a backend+runner,
    else the declared fallback probe (unit tests without a sandbox)."""
    backend = getattr(plane, "backend", None)
    runner_cmd = getattr(plane, "runner_cmd", None)
    probe: CapabilityProbe
    if backend is not None and runner_cmd:
        probe = SandboxCapabilityProbe(backend, runner_cmd)
    else:
        probe = DeclaredCapabilityProbe()
    return CapabilityCatalog(
        probe,
        get_account=registry.get,
        get_blob=registry.get_credential_blob,
    )

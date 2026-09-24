"""Independent cloud credential lifecycle (SOR-176).

Local CLI credentials and SBX cloud credentials are *independent
lifecycles*: a user's ``~/.codex/auth.json`` (et al.) is an import source
only — cloud refresh never writes back to local files. What the cloud
lane owns is the stored credential blob (``AccountStore`` credential
lane) plus the deployment-managed ``sbx-acct-<id>`` Secret. One manual
authorization can then run long-term: for OAuth providers the official
CLI performs the refresh itself inside a throwaway sandbox, file
mutation is detected by fingerprint, and the rotated bundle is
version-safely written back (blob files are atomic tmp+rename, Secret
refresh is recreate-in-place, every commit bumps the stored
``generation`` and is serialized by the same per-account lock family as
``CredentialSync.writeback``).

``invalid_grant``/``token_expired``/revoked refresh transitions an
account to ``reauth_required``/``revoked`` and marks it ``invalid`` so
the scheduler fails over — one 401 marks instead of an infinite 401
loop. Static credentials (OpenCode Zen ``api`` entries, Devin key files)
have no refresh channel, so they never acquire a refresh worker:
``credential_kind`` classifies a stored blob and the refresher returns
``skipped:static_credential`` before spending any exec.

Non-secret lifecycle metadata (state, kind, expiry epoch, fingerprint,
generation, counters — never tokens) is served at
``GET /v1/accounts/{id}/lifecycle``.
"""

from __future__ import annotations

import base64
import json
import os
import random
import shlex
import sys
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from control.accounts import is_valid_account_id
from control.config import account_secret_prefix
from control.credsync import TAG_CRED_BASE_FP, CredentialSync, blob_fingerprint
from control.onboarding import (
    _PROVIDER_BINS,
    PROVIDER_AUTH_CHECKS,
    classify_auth_output,
)
from control.sandbox_io import sandbox_env

# Canonical lifecycle states — published in docs/providers.md.
LIFECYCLE_STATES = (
    "healthy",
    "access_expiring",
    "refreshing",
    "healthy_refreshed",
    "reauth_required",
    "revoked",
)

TERMINAL_STATES = frozenset({"reauth_required", "revoked"})

_ACCESS_EXPIRING_WINDOW_S = int(os.environ.get("SBX_ACCESS_EXPIRING_WINDOW_S", "1800"))
_REFRESH_STALE_S = int(os.environ.get("SBX_CRED_REFRESH_STALE_S", "1800"))
_SCAN_INTERVAL_S = float(os.environ.get("SBX_CRED_REFRESH_INTERVAL_S", "300"))
_SCAN_JITTER_S = float(os.environ.get("SBX_CRED_REFRESH_JITTER_S", "60"))
# Accounts with no readable expiry get a periodic probe anyway — but never
# more often than this gap, so scans don't spam real (billable) API calls.
_MIN_REFRESH_GAP_S = int(os.environ.get("SBX_CRED_REFRESH_MIN_GAP_S", "3600"))

# Signatures that mean the *grant* is dead (not a transient outage). Only a
# classified terminal marker may flip ``reauth_required`` → ``revoked`` so a
# rebooted/healthy account cannot be wrongly condemned by a network blip.
_REVOKED_MARKERS = (
    "invalid_grant",
    "invalid refresh token",
    "invalid_refresh_token",
    "token_expired",
    "token expired",
    "refresh token expired",
    "revoked",
)

# --------------------------------------------------------------------------
# blob inspection — never returns token material, only shapes/epochs/kinds
# --------------------------------------------------------------------------

_EPOCH_S_CEILING = 10_000_000_000  # above this an epoch is milliseconds


def _decode_jwt_exp(value: Any) -> int | None:
    """Extract the ``exp`` claim from a JWT string without verifying it."""
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except Exception:
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    return int(exp) if isinstance(exp, (int, float)) else None


def _parse_iso_epoch(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        text = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def _as_epoch_s(value: Any) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    ivalue = int(value)
    if ivalue > _EPOCH_S_CEILING:
        ivalue //= 1000
    return ivalue


def _blob_documents(blob: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Decode every JSON file in a credential blob into a dict (or skip it)."""
    files = blob.get("files") if isinstance(blob, Mapping) else None
    if not isinstance(files, dict):
        return []
    docs: list[dict[str, Any]] = []
    for entry in files.values():
        if isinstance(entry, dict):
            raw = entry.get("content")
            if raw is None and entry.get("content_b64"):
                try:
                    raw = base64.b64decode(str(entry["content_b64"])).decode("utf-8", "replace")
                except Exception:
                    raw = None
        else:
            raw = entry
        if not isinstance(raw, str):
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict):
            docs.append(data)
    return docs


def _has_oauth_material(node: Any) -> bool:
    """True when the blob shape carries a refresh grant the CLI can rotate."""
    if isinstance(node, Mapping):
        if node.get("type") == "oauth":
            return True
        if ("refresh_token" in node or "refresh" in node) and any(
            k in node for k in ("access_token", "access", "key", "tokens")
        ):
            return True
        return any(_has_oauth_material(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_oauth_material(v) for v in node)
    return False


def credential_kind(provider: str, blob: Mapping[str, Any] | None) -> str:
    """``oauth`` when the bundle holds refresh material, else ``api_key``.

    A missing/unreadable blob defaults to ``oauth`` — the conservative
    choice: unknown providers may still refresh, and api_key is only
    asserted when the blob positively lacks refresh material.
    """
    if blob is None or provider == "devin":
        return "oauth" if blob is None else "api_key"
    if provider == "opencode":
        # Mixed blob: only an oauth entry justifies a refresh worker.
        return "oauth" if any(_has_oauth_material(d) for d in _blob_documents(blob)) else "api_key"
    return "oauth" if any(_has_oauth_material(d) for d in _blob_documents(blob)) else "api_key"


def _generic_expiry(data: Mapping[str, Any]) -> int | None:
    for key in ("access_token", "access", "key", "id_token"):
        exp = _decode_jwt_exp(data.get(key))
        if exp is not None:
            return exp
    for key in ("expiry", "expires_at", "expires"):
        exp = _as_epoch_s(data.get(key)) or _parse_iso_epoch(data.get(key))
        if exp is not None:
            return exp
    return None


def _doc_expiry(provider: str, data: Mapping[str, Any]) -> int | None:
    if provider == "codex":
        tokens = data.get("tokens") or {}
        if isinstance(tokens, Mapping):
            exp = _decode_jwt_exp(tokens.get("access_token"))
            if exp is not None:
                return exp
        return _generic_expiry(tokens if isinstance(tokens, Mapping) else data)
    if provider == "grok":
        # ``{<issuer>: {key(JWT), refresh_token, expires_at(iso)}}`` — earliest
        # expiry across entries is the one that matters.
        candidates: list[int] = []
        for value in data.values():
            if isinstance(value, Mapping):
                exp = _parse_iso_epoch(value.get("expires_at")) or _decode_jwt_exp(value.get("key"))
                if exp is not None:
                    candidates.append(exp)
        return min(candidates) if candidates else _generic_expiry(data)
    if provider == "antigravity":
        token = data.get("token") or {}
        if isinstance(token, Mapping):
            exp = _parse_iso_epoch(token.get("expiry")) or _decode_jwt_exp(
                token.get("access_token")
            )
            if exp is not None:
                return exp
        return _generic_expiry(data)
    if provider == "opencode":
        candidates = []
        for value in data.values():
            if isinstance(value, Mapping) and value.get("type") == "oauth":
                exp = _as_epoch_s(value.get("expires")) or _decode_jwt_exp(value.get("access"))
                if exp is not None:
                    candidates.append(exp)
        return min(candidates) if candidates else None
    return _generic_expiry(data)


def credential_expiry_epoch(provider: str, blob: Mapping[str, Any] | None) -> int | None:
    """Earliest access-token expiry across the blob's files (epoch seconds)."""
    exps = [
        exp
        for doc in _blob_documents(blob or {})
        if (exp := _doc_expiry(provider, doc)) is not None
    ]
    return min(exps) if exps else None


def grant_revoked(detail: str | None) -> bool:
    """True when ``detail`` carries a dead-grant signature."""
    if not detail:
        return False
    low = detail.lower()
    return any(marker in low for marker in _REVOKED_MARKERS)


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------


class CredentialLifecycleService:
    """Reads/writes the non-secret credential lifecycle record per account.

    ``registry_source`` is a ``PersistentAccountRegistry`` or a callable
    returning one (mirroring ``CredentialSync``'s lazy resolution). Stores
    without the lifecycle lane fall back to in-memory records, so the
    service works against plain fakes too.
    """

    def __init__(
        self,
        registry_source: Any,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._source = registry_source
        self._now = now
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._memo: dict[str, dict[str, Any]] = {}

    # -- store plumbing -----------------------------------------------------

    def _registry(self) -> Any | None:
        try:
            return self._source() if callable(self._source) else self._source
        except Exception:
            return None

    def lock_for(self, account_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(account_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[account_id] = lock
            return lock

    def _read(self, account_id: str) -> dict[str, Any] | None:
        registry = self._registry()
        reader = getattr(registry, "get_credential_lifecycle", None) if registry else None
        try:
            rec = reader(account_id) if reader is not None else None
        except Exception:
            rec = None
        if rec is None:
            rec = self._memo.get(account_id)
        return dict(rec) if isinstance(rec, dict) else None

    def _write(self, account_id: str, rec: dict[str, Any]) -> None:
        registry = self._registry()
        writer = getattr(registry, "put_credential_lifecycle", None) if registry else None
        try:
            if writer is not None:
                writer(account_id, dict(rec))
                return
        except Exception:
            pass
        self._memo[account_id] = dict(rec)

    # -- transitions ---------------------------------------------------------

    def note_credential(self, account_id: str, blob: Mapping[str, Any] | None) -> dict[str, Any]:
        """Record (or re-record) a freshly imported credential bundle.

        A *changed* fingerprint clears terminal states (a new grant was
        imported); the same bundle over a revoked record keeps the flag.
        """
        with self.lock_for(account_id):
            rec = self._read(account_id) or {}
            fp = blob_fingerprint(blob)
            kind = credential_kind(self._provider(account_id), blob)
            expires = credential_expiry_epoch(self._provider(account_id), blob)
            same = fp is not None and fp == rec.get("fingerprint")
            rec.update(
                {
                    "account_id": account_id,
                    "kind": kind,
                    "fingerprint": fp,
                    "expires_at": expires,
                    "imported_at": int(self._now()),
                }
            )
            if fp is not None and not same:
                rec["generation"] = int(rec.get("generation") or 0) + 1
            if not (same and rec.get("state") in TERMINAL_STATES):
                rec["state"] = "access_expiring" if self._expiring(expires) else "healthy"
                rec.pop("last_error", None)
            self._write(account_id, rec)
            return dict(rec)

    def begin_refresh(self, account_id: str) -> bool:
        """Claim the refresh slot: false when another refresh is in flight.

        A ``refreshing`` claim older than ``_REFRESH_STALE_S`` is treated as
        crashed and can be re-claimed.
        """
        with self.lock_for(account_id):
            rec = self._read(account_id) or {"account_id": account_id}
            if rec.get("state") == "refreshing":
                started = rec.get("refresh_started_at") or 0
                if self._now() - float(started) < _REFRESH_STALE_S:
                    return False
            rec["state"] = "refreshing"
            rec["refresh_started_at"] = int(self._now())
            self._write(account_id, rec)
            return True

    def finish_refresh(
        self,
        account_id: str,
        *,
        outcome: str,
        blob: Mapping[str, Any] | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a ``refreshing`` claim after a probe/write-back.

        ``outcome`` is a ``WritebackOutcome`` code, ``ok``, ``auth_invalid``,
        ``revoked``, or an ``error:*`` tag for transient probe failures.
        """
        with self.lock_for(account_id):
            rec = self._read(account_id) or {"account_id": account_id}
            rec.pop("refresh_started_at", None)
            rec["last_refresh_at"] = int(self._now())
            provider = self._provider(account_id)
            if outcome == "committed" and blob is not None:
                fp = blob_fingerprint(blob)
                # Idempotent on fingerprint: the write-back's commit hook and
                # the refresher's resolution can both report one rotation.
                if rec.get("fingerprint") != fp or rec.get("state") != "healthy_refreshed":
                    rec["generation"] = int(rec.get("generation") or 0) + 1
                rec["state"] = "healthy_refreshed"
                rec["fingerprint"] = fp
                rec["expires_at"] = credential_expiry_epoch(provider, blob)
                rec["kind"] = credential_kind(provider, blob)
                rec["last_refreshed_at"] = int(self._now())
                rec.pop("last_error", None)
            elif outcome in ("auth_invalid", "revoked"):
                rec["state"] = "revoked" if outcome == "revoked" else "reauth_required"
                rec["last_error"] = outcome
            elif outcome.startswith("error:"):
                rec["last_error"] = outcome
                if rec.get("state") == "refreshing" or not rec.get("state"):
                    rec["state"] = self._derive(account_id, rec)
            else:  # ok / unchanged / stale / skipped:*
                if rec.get("state") in ("refreshing", None):
                    rec["state"] = self._derive(account_id, rec)
                rec.pop("last_error", None)
            self._write(account_id, rec)
            return dict(rec)

    def on_auth_invalid(
        self,
        account_id: str,
        *,
        detail: str | None = None,
        mark_account: bool = True,
    ) -> dict[str, Any] | None:
        """Terminal credential rejection → reauth_required/revoked + failover.

        ``detail`` is an already-redacted error string; only its marker
        class is stored, never the raw text.
        """
        with self.lock_for(account_id):
            rec = self._read(account_id) or {"account_id": account_id}
            rec["state"] = "revoked" if grant_revoked(detail) else "reauth_required"
            rec["last_error"] = "invalid_grant" if rec["state"] == "revoked" else "auth_invalid"
            rec["last_auth_invalid_at"] = int(self._now())
            self._write(account_id, rec)
            out = dict(rec)
        if mark_account:
            registry = self._registry()
            try:
                if registry is not None:
                    registry.mark_status(account_id, "invalid", last_error=out["last_error"])
            except Exception:
                pass
        return out

    # -- reads ----------------------------------------------------------------

    def describe(self, account_id: str) -> dict[str, Any]:
        """Non-secret lifecycle metadata for one account (public-shape)."""
        registry = self._registry()
        account = None
        blob = None
        try:
            if registry is not None:
                account = registry.get(account_id)
                blob = registry.get_credential_blob(account_id)
        except Exception:
            pass
        provider = getattr(account, "provider", None) or (
            blob.get("provider") if isinstance(blob, dict) else None
        )
        rec = self._read(account_id) or {}
        stored_fp = rec.get("fingerprint")
        live_fp = blob_fingerprint(blob)
        if stored_fp and live_fp and stored_fp != live_fp and rec.get("state") in TERMINAL_STATES:
            # The credential was replaced since the terminal flag — re-derive.
            rec.pop("state", None)
        kind = rec.get("kind") or credential_kind(provider or "", blob)
        expires = rec.get("expires_at")
        if expires is None:
            expires = credential_expiry_epoch(provider or "", blob)
        state = rec.get("state")
        if state == "refreshing":
            started = rec.get("refresh_started_at") or 0
            if self._now() - float(started) >= _REFRESH_STALE_S:
                state = None
        if state in TERMINAL_STATES:
            pass
        elif self._expiring(expires):
            state = "access_expiring"
        elif not state:
            state = "healthy_refreshed" if rec.get("generation") else "healthy"
        refresh_due = (
            state == "access_expiring"
            or (
                expires is None
                and kind == "oauth"
                and (self._now() - float(rec.get("last_refresh_at") or 0))
                >= _MIN_REFRESH_GAP_S
            )
        )
        return {
            "account_id": account_id,
            "provider": provider,
            "kind": kind,
            "state": state,
            "refresh_due": refresh_due,
            "expires_at": expires,
            "fingerprint": rec.get("fingerprint") or live_fp,
            "generation": int(rec.get("generation") or 0),
            "imported_at": rec.get("imported_at"),
            "last_refresh_at": rec.get("last_refresh_at"),
            "last_refreshed_at": rec.get("last_refreshed_at"),
            "last_error": rec.get("last_error"),
            "secret_managed": bool(
                getattr(account, "secret_name", None)
                and getattr(account, "secret_name", "").startswith(account_secret_prefix())
            ),
            "refreshable": kind == "oauth" and blob is not None,
        }

    def _provider(self, account_id: str) -> str:
        registry = self._registry()
        try:
            account = registry.get(account_id) if registry is not None else None
            provider = getattr(account, "provider", None)
            if provider:
                return provider
            blob = registry.get_credential_blob(account_id) if registry is not None else None
            if isinstance(blob, dict) and blob.get("provider"):
                return str(blob["provider"])
        except Exception:
            pass
        return ""

    def _expiring(self, expires: int | None) -> bool:
        return expires is not None and float(expires) <= self._now() + _ACCESS_EXPIRING_WINDOW_S

    def _derive(self, account_id: str, rec: Mapping[str, Any]) -> str:
        expires = rec.get("expires_at")
        if expires is None:
            registry = self._registry()
            try:
                blob = registry.get_credential_blob(account_id) if registry else None
            except Exception:
                blob = None
            expires = credential_expiry_epoch(self._provider(account_id), blob)
        if self._expiring(expires):
            return "access_expiring"
        return "healthy_refreshed" if rec.get("generation") else "healthy"


# --------------------------------------------------------------------------
# proactive refresher
# --------------------------------------------------------------------------

_REFRESH_ARGV_TAILS: dict[str, tuple[str, ...]] = {
    # The documented auth checks are read-only for codex/opencode — a real,
    # minimal API call is what exercises the CLI's lazy-refresh path.
    "codex": ("exec", "--skip-git-repo-check", "Reply with exactly: ok"),
    "antigravity": ("models",),
    "grok": ("models",),
    "devin": ("auth", "status"),
}


def provider_refresh_argv(
    provider: str,
    *,
    model: str | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str] | None:
    """Argv for the CLI call that exercises the provider's refresh path."""
    if provider == "opencode":
        tail: tuple[str, ...] = (
            ("run", "-m", model, "Reply with exactly: ok") if model else ("auth", "list")
        )
    else:
        tail = _REFRESH_ARGV_TAILS.get(provider) or PROVIDER_AUTH_CHECKS.get(provider)
    if tail is None:
        return None
    env = os.environ if env is None else env
    bin_env, default_bin = _PROVIDER_BINS.get(provider, ("", provider))
    tokens = shlex.split(env.get(bin_env) or default_bin)
    if not tokens:
        return None
    if len(tokens) == 1 and tokens[0].endswith(".py"):
        return [sys.executable, tokens[0], *tail]
    return [*tokens, *tail]


def worker_enabled(backend_kind: str) -> bool:
    """Worker runs wherever a store write-back can land (any backend)."""
    return os.environ.get("SBX_CRED_REFRESH", "1") != "0"


class CredentialRefresher:
    """Periodic per-account OAuth refresh via the official CLI.

    Each scan picks ``oauth`` accounts whose stored blob is expiring (or in
    ``healthy``/``access_expiring`` state), spawns a throwaway sandbox, runs
    the provider's refresh-triggering call, and hands the sandbox to
    ``CredentialSync.writeback`` — the same CAS/commit/Secret-refresh path
    used after real turns.
    """

    def __init__(
        self,
        *,
        registry_source: Callable[[], Any | None],
        backend: Any,
        runner_cmd: list[str],
        sync: CredentialSync,
        lifecycle: CredentialLifecycleService,
        interval_s: float = _SCAN_INTERVAL_S,
        jitter_s: float = _SCAN_JITTER_S,
        default_model: str = "gpt-5.6-luna",
        spec_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self._registry_source = registry_source
        self._backend = backend
        self._runner_cmd = list(runner_cmd)
        self._sync = sync
        self._lifecycle = lifecycle
        self._interval_s = interval_s
        self._jitter_s = jitter_s
        self._default_model = default_model
        self._spec_factory = spec_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- thread --------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="sbx-cred-refresher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                pass
            self._stop.wait(self._interval_s + random.uniform(0, self._jitter_s))

    # -- scan ------------------------------------------------------------------

    def scan_once(self) -> list[dict[str, Any]]:
        registry = self._registry()
        if registry is None:
            return []
        try:
            accounts = list(registry.list())
        except Exception:
            return []
        results = []
        for account in accounts:
            if getattr(account, "status", None) in ("disabled",):
                continue
            try:
                result = self.refresh_account(account.id, account=account)
            except Exception:
                result = {"account_id": account.id, "result": "error:scan"}
            results.append(result)
        return results

    def refresh_account(self, account_id: str, *, account: Any = None) -> dict[str, Any]:
        registry = self._registry()
        if registry is None:
            return {"account_id": account_id, "result": "skipped:no_registry"}
        if account is None:
            try:
                account = registry.get(account_id)
            except Exception:
                account = None
        if account is None or not is_valid_account_id(account_id):
            return {"account_id": account_id, "result": "skipped:account"}
        try:
            blob = registry.get_credential_blob(account_id)
        except Exception:
            blob = None
        provider = getattr(account, "provider", "")
        if blob is None:
            return {"account_id": account_id, "result": "skipped:no_blob"}
        if credential_kind(provider, blob) != "oauth":
            # api_key material (OpenCode Zen entries, Devin keys): no refresh
            # channel exists, so no worker exec is ever spent on it.
            return {"account_id": account_id, "result": "skipped:static_credential"}
        lifecycle = self._lifecycle.describe(account_id)
        if lifecycle["state"] in TERMINAL_STATES:
            return {"account_id": account_id, "result": "skipped:terminal"}
        if not lifecycle.get("refresh_due"):
            return {"account_id": account_id, "result": "skipped:not_due"}
        if not self._lifecycle.begin_refresh(account_id):
            return {"account_id": account_id, "result": "skipped:in_flight"}

        handle = None
        outcome = "error:unknown"
        try:
            handle = self._create_sandbox(account, blob)
            env = self._sandbox_env(handle, account, blob)
            outcome = self._probe_init(handle, account, env)
            if outcome == "ok":
                outcome = self._probe_auth(provider, account, handle, env)
            if outcome == "ok":
                wb = self._sync.writeback(
                    backend=self._backend,
                    handle=handle,
                    runner_cmd=self._runner_cmd,
                    tags={
                        "account_id": account_id,
                        TAG_CRED_BASE_FP: blob_fingerprint(blob),
                    },
                )
                outcome = wb.code
        except Exception:
            outcome = "error:probe"
        finally:
            if handle is not None:
                try:
                    self._backend.terminate(handle)
                except Exception:
                    pass
        if outcome in ("auth_invalid", "revoked"):
            self._lifecycle.on_auth_invalid(account_id, detail=outcome)
        else:
            committed_blob = None
            if outcome == "committed":
                try:
                    committed_blob = registry.get_credential_blob(account_id)
                except Exception:
                    committed_blob = None
            self._lifecycle.finish_refresh(account_id, outcome=outcome, blob=committed_blob)
        return {
            "account_id": account_id,
            "result": outcome,
            "lifecycle": self._lifecycle.describe(account_id),
        }

    # -- sandbox plumbing --------------------------------------------------------

    def _create_sandbox(self, account: Any, blob: Mapping[str, Any]) -> Any:
        if self._spec_factory is not None:
            spec = self._spec_factory(account)
        else:
            from control.backend import SandboxSpec

            secrets = [account.secret_name] if getattr(account, "secret_name", None) else []
            spec = SandboxSpec(
                tags={
                    "purpose": "credential_refresh",
                    "provider": account.provider,
                    "account_id": account.id,
                },
                secrets=secrets,
            )
        return self._backend.create(spec)

    def _sandbox_env(self, handle: Any, account: Any, blob: Mapping[str, Any]) -> dict[str, str]:
        env = sandbox_env(
            handle,
            {"SBX_ACCOUNT_ID": account.id, "SBX_ACCOUNT_CREDENTIAL": json.dumps(blob)},
        )
        return env

    def _model_for(self, account: Any) -> str:
        models = getattr(account, "models", None) or []
        return str(models[0]) if models else self._default_model

    def _probe_init(self, handle: Any, account: Any, env: dict[str, str]) -> str:
        argv = [
            *self._runner_cmd,
            "init",
            "--auth",
            "auth_json",
            "--model",
            self._model_for(account),
            "--provider",
            account.provider,
            "--account-id",
            account.id,
        ]
        try:
            proc = self._backend.exec(handle, argv, env=env)
            for _ in proc.stdout:
                pass
            code = proc.wait()
        except Exception:
            return "error:init_failed"
        return "ok" if code == 0 else "error:init_failed"

    def _probe_auth(self, provider: str, account: Any, handle: Any, env: dict[str, str]) -> str:
        argv = provider_refresh_argv(
            provider,
            model=self._model_for(account) if provider == "opencode" else None,
        )
        if argv is None:
            return "error:probe_unavailable"
        try:
            proc = self._backend.exec(handle, argv, env=env)
            chunks: list[str] = []
            size = 0
            for line in proc.stdout:
                size += len(line)
                if size > 65536:
                    break
                chunks.append(line)
            code = proc.wait()
        except Exception:
            return "error:probe_failed"
        output = "\n".join(chunks)
        outcome = classify_auth_output(provider, code, output)
        if outcome == "auth_invalid" and grant_revoked(output):
            return "revoked"
        return "ok" if outcome == "ok" else outcome

    def _registry(self) -> Any | None:
        try:
            return self._registry_source()
        except Exception:
            return None

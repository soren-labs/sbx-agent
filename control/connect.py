"""SOR-214: Provider Connect — hosted/paired auth sessions over the
canonical engine (``control.provider_auth.AuthService`` + adapters).

A connect session drives the provider's *own* login flow to a captured,
imported + verified account — no token/JSON paste anywhere. Two lanes:

* ``hosted`` — the control-plane host can exec the provider CLI itself
  (local backend with a CLI on PATH): the official login runs under a
  per-session scratch ``$HOME``, its output is scanned for browser/
  device URLs to surface live in the Console, and on exit the scratch
  credential is captured → imported/relinked → cloud verify probe.
* ``pair`` — pure cloud deploys cannot run an interactive login (no
  provider CLI on the plane): the session mints a single-use short-TTL
  ticket and a local machine runs ``sbx auth pair <ticket>`` — the CLI
  executes the vendor login where it runs, then POSTs the captured blob
  to ``/v1/auth/pair/complete`` (the ticket itself is the credential —
  same threat model as ``/v1/console/exchange`` grants).

Session states reuse the canonical ``AUTH_SESSION_STATES`` vocabulary —
``authenticating`` while in flight, ``verified``/``materialized`` on a
completed import — plus session-only terminals ``failed`` /
``cancelled`` / ``expired``. Sessions carry metadata only: credential
blobs pass straight into the store lane and are never kept on the
session record or returned by any read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from control.accounts import PersistentAccountRegistry
from control.onboarding import (
    OnboardingError,
    OnboardingService,
    validate_credential_blob,
)
from control.provider_auth import (
    AUTH_SESSION_STATES,
    AuthService,
    adapter_for,
    host_cli_env,
    provider_login_argv,
)

CONNECT_DICT_ENV = "SBX_CONNECT_DICT"
CONNECT_STORE_DIR_ENV = "SBX_CONNECT_STORE_DIR"
DEFAULT_CONNECT_DICT = "sbx-connect"

CONNECT_SESSION_TTL_S = 900.0
# Session-only terminals on top of the canonical auth-session vocabulary —
# the account's own ``auth_state`` only ever takes AUTH_SESSION_STATES.
CONNECT_STATES: tuple[str, ...] = AUTH_SESSION_STATES + (
    "failed",
    "cancelled",
    "expired",
)
_TERMINAL = frozenset({"verified", "materialized", "failed", "cancelled", "expired"})

# Login output scraping: the first http(s) URL is the browser/device link;
# the first XXXX-XXXX(-ish) token on a "code"/"verify" line is the user code.
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,5}|[A-Z0-9]{6,9})\b")
_CODE_LINE_RE = re.compile(r"code|verify|enter", re.IGNORECASE)


class ConnectError(Exception):
    """Structured Provider Connect refusal (code → HTTP mapping at /v1)."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# session record


@dataclass
class ConnectSession:
    """One provider-connect attempt — metadata only, never credential data."""

    id: str
    provider: str
    kind: str  # "hosted" | "pair"
    state: str  # AUTH_SESSION_STATES ∪ {"failed","cancelled","expired"}
    account_id: str | None = None  # relink target / created account id
    relink: bool = False
    label: str = ""
    slots: int = 1
    models: tuple[str, ...] = ()
    browser_url: str | None = None
    user_code: str | None = None
    pair_ticket: str | None = None  # in-memory only — never persisted
    pair_ticket_hash: str | None = None  # sha256 — what the store holds
    error: str | None = None
    created_at: str = ""
    expires_at: float = 0.0
    updated_at: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "provider": self.provider,
            "kind": self.kind,
            "state": self.state,
            "account_id": self.account_id,
            "relink": self.relink,
            "label": self.label,
            "browser_url": self.browser_url,
            "user_code": self.user_code,
            "error": self.error,
            "created_at": self.created_at,
            "expires_at": _iso(self.expires_at),
            "updated_at": self.updated_at,
        }
        # The pair ticket is a bearer credential — only expose it while the
        # session is still redeemable, and only to the (admin) reader.
        if self.kind == "pair" and self.state == "authenticating" and self.pair_ticket:
            out["pair_ticket"] = self.pair_ticket
            out["pair_command"] = f"sbx auth pair {self.pair_ticket}"
        return out


def session_to_dict(session: ConnectSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "provider": session.provider,
        "kind": session.kind,
        "state": session.state,
        "account_id": session.account_id,
        "relink": session.relink,
        "label": session.label,
        "slots": session.slots,
        "models": list(session.models),
        "browser_url": session.browser_url,
        "user_code": session.user_code,
        "pair_ticket_hash": session.pair_ticket_hash,
        "error": session.error,
        "created_at": session.created_at,
        "expires_at": session.expires_at,
        "updated_at": session.updated_at,
    }


def session_from_dict(data: Any) -> ConnectSession | None:
    if not isinstance(data, dict):
        return None
    try:
        return ConnectSession(
            id=str(data["id"]),
            provider=str(data["provider"]),
            kind=str(data["kind"]),
            state=str(data["state"]),
            account_id=data.get("account_id"),
            relink=bool(data.get("relink")),
            label=str(data.get("label") or ""),
            slots=int(data.get("slots") or 1),
            models=tuple(data.get("models") or ()),
            browser_url=data.get("browser_url"),
            user_code=data.get("user_code"),
            pair_ticket_hash=data.get("pair_ticket_hash"),
            error=data.get("error"),
            created_at=str(data.get("created_at") or ""),
            expires_at=float(data.get("expires_at") or 0.0),
            updated_at=str(data.get("updated_at") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# durable store — sessions + hashed pair tickets


class ConnectSessionStore(Protocol):
    """Connect sessions keyed by id, plus single-use pair tickets.

    Tickets are stored hashed (``sha256``) — the store never holds a
    redeemable ticket, mirroring ``ConsoleGrantStore``.
    """

    def put(self, session: ConnectSession) -> None: ...
    def get(self, session_id: str) -> ConnectSession | None: ...
    def delete(self, session_id: str) -> None: ...
    def list(self) -> list[ConnectSession]: ...
    def put_ticket(self, ticket_hash: str, session_id: str, expires_epoch: float) -> None: ...
    def get_ticket(self, ticket_hash: str) -> tuple[str, float] | None: ...
    def pop_ticket(self, ticket_hash: str) -> tuple[str, float] | None: ...


class InMemoryConnectStore:
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._tickets: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def put(self, session: ConnectSession) -> None:
        with self._lock:
            self._sessions[session.id] = session_to_dict(session)

    def get(self, session_id: str) -> ConnectSession | None:
        with self._lock:
            raw = self._sessions.get(session_id)
        return session_from_dict(raw) if raw is not None else None

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def list(self) -> list[ConnectSession]:
        with self._lock:
            items = list(self._sessions.values())
        out = [session_from_dict(s) for s in items]
        return sorted((s for s in out if s is not None), key=lambda s: s.created_at, reverse=True)

    def put_ticket(self, ticket_hash: str, session_id: str, expires_epoch: float) -> None:
        with self._lock:
            self._tickets[ticket_hash] = (session_id, expires_epoch)

    def get_ticket(self, ticket_hash: str) -> tuple[str, float] | None:
        with self._lock:
            return self._tickets.get(ticket_hash)

    def pop_ticket(self, ticket_hash: str) -> tuple[str, float] | None:
        with self._lock:
            return self._tickets.pop(ticket_hash, None)


class FileConnectStore:
    """JSON-per-session store for the local (non-Modal) control plane."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    def _path(self, session_id: str) -> Path:
        return self._root / f"session-{session_id}.json"

    def _tickets_path(self) -> Path:
        return self._root / "tickets.json"

    def put(self, session: ConnectSession) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(session.id)
        with self._lock:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(session_to_dict(session)), encoding="utf-8")
            tmp.replace(path)

    def get(self, session_id: str) -> ConnectSession | None:
        try:
            raw = self._path(session_id).read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return session_from_dict(json.loads(raw))
        except json.JSONDecodeError:
            return None

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._path(session_id).unlink(missing_ok=True)

    def list(self) -> list[ConnectSession]:
        out: list[ConnectSession] = []
        paths = sorted(self._root.glob("session-*.json")) if self._root.is_dir() else []
        for path in paths:
            try:
                rec = session_from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
            if rec is not None:
                out.append(rec)
        out.sort(key=lambda s: s.created_at, reverse=True)
        return out

    def put_ticket(self, ticket_hash: str, session_id: str, expires_epoch: float) -> None:
        with self._lock:
            tickets = self._read_tickets()
            tickets[ticket_hash] = {"session_id": session_id, "expires": expires_epoch}
            self._write_tickets(tickets)

    def get_ticket(self, ticket_hash: str) -> tuple[str, float] | None:
        with self._lock:
            return self._ticket_entry(self._read_tickets(), ticket_hash)

    def pop_ticket(self, ticket_hash: str) -> tuple[str, float] | None:
        with self._lock:
            tickets = self._read_tickets()
            entry = self._ticket_entry(tickets, ticket_hash)
            if entry is not None:
                tickets.pop(ticket_hash)
                self._write_tickets(tickets)
            return entry

    @staticmethod
    def _ticket_entry(tickets: dict[str, Any], ticket_hash: str) -> tuple[str, float] | None:
        raw = tickets.get(ticket_hash)
        if not isinstance(raw, dict):
            return None
        try:
            return str(raw["session_id"]), float(raw["expires"])
        except (KeyError, TypeError, ValueError):
            return None

    def _read_tickets(self) -> dict[str, Any]:
        try:
            data = json.loads(self._tickets_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_tickets(self, tickets: dict[str, Any]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._tickets_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(tickets), encoding="utf-8")
        tmp.replace(path)


class ModalDictConnectStore:
    """Production store backed by ``modal.Dict`` — lazy-imports modal."""

    def __init__(self, name: str = DEFAULT_CONNECT_DICT) -> None:
        self._name = name
        self._dict: Any = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def put(self, session: ConnectSession) -> None:
        self._d()[f"session:{session.id}"] = session_to_dict(session)

    def get(self, session_id: str) -> ConnectSession | None:
        raw = self._d().get(f"session:{session_id}")
        return session_from_dict(raw)

    def delete(self, session_id: str) -> None:
        try:
            self._d().pop(f"session:{session_id}")
        except KeyError:
            pass

    def list(self) -> list[ConnectSession]:
        out: list[ConnectSession] = []
        for key, raw in self._d().items():
            if str(key).startswith("session:"):
                rec = session_from_dict(raw)
                if rec is not None:
                    out.append(rec)
        out.sort(key=lambda s: s.created_at, reverse=True)
        return out

    def put_ticket(self, ticket_hash: str, session_id: str, expires_epoch: float) -> None:
        self._d()[f"ticket:{ticket_hash}"] = {
            "session_id": session_id,
            "expires": expires_epoch,
        }

    def get_ticket(self, ticket_hash: str) -> tuple[str, float] | None:
        raw = self._d().get(f"ticket:{ticket_hash}")
        if not isinstance(raw, dict):
            return None
        try:
            return str(raw["session_id"]), float(raw["expires"])
        except (KeyError, TypeError, ValueError):
            return None

    def pop_ticket(self, ticket_hash: str) -> tuple[str, float] | None:
        try:
            raw = self._d().pop(f"ticket:{ticket_hash}")
        except KeyError:
            return None
        if not isinstance(raw, dict):
            return None
        try:
            return str(raw["session_id"]), float(raw["expires"])
        except (KeyError, TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# the shared throwaway-sandbox auth probe (extracted from POST
# /v1/accounts/{id}/verify so Provider Connect completion runs it identically)

VERIFY_PURPOSE_TAG = "account-verify"


def probe_account_credential(
    plane: Any,
    registry: Any,
    account: Any,
    *,
    model: str | None = None,
) -> Any:
    """Probe the stored credential in a throwaway sandbox; fold the verdict.

    Runs ``runner init --auth auth_json`` with the account's credential
    attached: the named Modal Secret when ``secret_name`` is set, else the
    registry blob via ``SBX_ACCOUNT_CREDENTIAL``. Exit 0 records verified
    evidence and promotes to ``active``; 5 → ``auth_invalid``; other
    non-zero → ``init_failed``; unreachable backend keeps the status and
    only records ``probe_unavailable`` (SOR-216 verified-only lifecycle).
    """
    from control.credlifecycle import CredentialLifecycleService
    from control.sandbox_io import sandbox_env

    account_id = account.id
    blob = registry.get_credential_blob(account_id)
    backend = getattr(plane, "backend", None)
    runner = getattr(plane, "runner", None)
    if backend is None or runner is None:
        return registry.mark_status(
            account_id,
            account.status,
            cooldown_until=account.cooldown_until,
            last_error="probe_unavailable",
        )
    if not blob and not account.secret_name:
        # A deployment-wide default secret is not this account's credential.
        return registry.mark_status(account_id, account.status, last_error="credential_missing")
    handle = None
    try:
        from control.backend import SandboxSpec

        # Secret-only accounts carry their credential in the named Modal
        # Secret; a local registry blob travels via SBX_ACCOUNT_CREDENTIAL.
        secrets_ = [account.secret_name] if account.secret_name else []
        handle = backend.create(
            SandboxSpec(
                tags={
                    "purpose": VERIFY_PURPOSE_TAG,
                    "provider": account.provider,
                    "account_id": account_id,
                },
                secrets=secrets_,
            )
        )
        verify_env: dict[str, str] = {"SBX_ACCOUNT_ID": account_id}
        if blob:
            verify_env["SBX_ACCOUNT_CREDENTIAL"] = json.dumps(blob)
        env = sandbox_env(handle, verify_env)
        if not blob:
            # An empty or unrelated blob would shadow the named Secret.
            env.pop("SBX_ACCOUNT_CREDENTIAL", None)
        use_model = model or getattr(plane, "default_model", None) or "gpt-5.6-luna"
        argv = runner(
            "init",
            "--auth",
            "auth_json",
            "--model",
            use_model,
            "--provider",
            account.provider,
            "--account-id",
            account_id,
        )
        proc = backend.exec(handle, argv, env=env)
        for _ in proc.stdout:
            pass
        code = proc.wait()
    except Exception:
        code = -1
    finally:
        if handle is not None:
            try:
                backend.terminate(handle)
            except Exception:
                pass
    try:
        lifecycle = CredentialLifecycleService(registry)
        if code == 0:
            if blob:
                lifecycle.note_credential(account_id, blob)
            # The sandbox probe accepted the materialized credential —
            # record cloud-verify evidence for the eligibility gate.
            lifecycle.note_verified(account_id, probe="v1:verify")
        elif code == 5:
            lifecycle.on_auth_invalid(account_id, detail="auth_invalid", mark_account=False)
    except Exception:
        pass
    if code == 0:
        return registry.mark_status(account_id, "active", last_error=None)
    if code == 5:
        return registry.mark_status(account_id, "invalid", last_error="auth_invalid")
    if code > 0:
        return registry.mark_status(account_id, "invalid", last_error="init_failed")
    return account


def plane_verify(
    plane: Any,
    registry: Any,
    *,
    model_for: Callable[[Any], str | None] | None = None,
) -> Callable[[str], bool]:
    """A ``verify(account_id) -> bool`` closure over the sandbox probe."""

    def _verify(account_id: str) -> bool:
        account = registry.get(account_id)
        if account is None:
            return False
        model = model_for(account) if model_for is not None else None
        updated = probe_account_credential(plane, registry, account, model=model)
        return getattr(updated, "status", "") == "active"

    return _verify


# ---------------------------------------------------------------------------
# service


def _write_blob_file(path: Path, blob: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(blob, ensure_ascii=False))


def _default_work_dir(env: Mapping[str, str]) -> Path:
    xdg = env.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path(env.get("HOME") or str(Path.home())) / ".local" / "state"
    return base / "sbx-browser" / "connect"


def default_connect_service(
    registry: PersistentAccountRegistry,
    *,
    env: Mapping[str, str] | None = None,
    store: ConnectSessionStore | None = None,
) -> ProviderConnectService:
    """The env-selected Provider Connect service (modal Dict / file store).

    The onboarding lane carries a Modal Secret writer only on Modal — same
    rule as the account-materialization path in ``control.app``.
    """
    env = dict(os.environ if env is None else env)
    backend = env.get("SBX_BACKEND") or "local"
    if store is None:
        if backend == "modal":
            store = ModalDictConnectStore(env.get(CONNECT_DICT_ENV) or DEFAULT_CONNECT_DICT)
        else:
            store = FileConnectStore(env.get(CONNECT_STORE_DIR_ENV) or _default_work_dir(env))
    secret_writer = None
    if backend == "modal":
        from control.credsync import ModalCredentialSecretWriter

        secret_writer = ModalCredentialSecretWriter()
    onboarding = OnboardingService(registry, secret_writer=secret_writer)
    return ProviderConnectService(
        registry,
        onboarding=onboarding,
        store=store,
        env=env,
    )


class ProviderConnectService:
    """Connect sessions over the canonical AuthService.

    ``hosted_available`` decides the lane: default is "local backend *and*
    the provider's login binary resolvable on PATH" — otherwise the pair
    lane. ``launcher`` (test seam) replaces the subprocess exec; it takes
    ``(argv, env, on_output)`` and returns the exit code.
    """

    def __init__(
        self,
        registry: PersistentAccountRegistry,
        *,
        onboarding: OnboardingService | None = None,
        store: ConnectSessionStore | None = None,
        env: Mapping[str, str] | None = None,
        work_dir: Path | str | None = None,
        hosted_available: Callable[[str], bool] | None = None,
        launcher: Callable[..., int] | None = None,
        clock: Callable[[], float] = time.time,
        ttl_s: float = CONNECT_SESSION_TTL_S,
    ) -> None:
        self._registry = registry
        self._auth = AuthService(registry, onboarding=onboarding, env=env)
        self._store = store or InMemoryConnectStore()
        self._env = dict(env if env is not None else os.environ)
        self._work_dir = Path(work_dir) if work_dir is not None else _default_work_dir(self._env)
        self._hosted_available = hosted_available or self._default_hosted_available
        self._launcher = launcher
        self._clock = clock
        self._ttl_s = ttl_s
        self._procs: dict[str, subprocess.Popen[Any]] = {}
        self._verify_fns: dict[str, Callable[[str], bool]] = {}
        self._lock = threading.Lock()

    # -- lane -------------------------------------------------------------

    def _default_hosted_available(self, provider: str) -> bool:
        """Hosted lane needs a local backend + the provider CLI on PATH."""
        if (self._env.get("SBX_BACKEND") or "local") == "modal":
            return False
        argv = provider_login_argv(provider, env=self._env)
        if not argv:
            return False
        if argv[0] == sys.executable and len(argv) > 1:
            return Path(argv[1]).is_file()
        return shutil.which(argv[0]) is not None or Path(argv[0]).is_file()

    # -- lifecycle ---------------------------------------------------------

    def begin(
        self,
        provider: str,
        *,
        label: str = "",
        slots: int = 1,
        models: list[str] | tuple[str, ...] | None = None,
        account_id: str | None = None,
        verify: Callable[[str], bool] | None = None,
    ) -> dict[str, Any]:
        """Open a connect session — hosted login or pair ticket."""
        adapter_for(provider)  # unknown provider → OnboardingError (400)
        relink = account_id is not None
        if relink:
            account = self._registry.get(str(account_id))
            if account is None:
                raise ConnectError(
                    "account_not_found",
                    f"account {account_id!r} not found",
                    status_code=404,
                )
            if account.provider != provider:
                raise ConnectError(
                    "provider_mismatch",
                    f"account {account_id!r} is provider {account.provider!r}, not {provider!r}",
                )
        kind = "hosted" if self._hosted_available(provider) else "pair"
        sid = f"conn-{secrets.token_hex(8)}"
        now = self._clock()
        session = ConnectSession(
            id=sid,
            provider=provider,
            kind=kind,
            state="authenticating",
            account_id=account_id,
            relink=relink,
            label=label,
            slots=max(1, int(slots)),
            models=tuple(models or ()),
            created_at=_iso(now),
            expires_at=now + self._ttl_s,
            updated_at=_iso(now),
        )
        if kind == "pair":
            session.pair_ticket = f"sbxp_{secrets.token_urlsafe(24)}"
            session.pair_ticket_hash = _ticket_hash(session.pair_ticket)
            self._store.put_ticket(session.pair_ticket_hash, sid, session.expires_at)
        self._store.put(session)
        if verify is not None:
            self._verify_fns[sid] = verify
        if kind == "hosted":
            threading.Thread(target=self._run_hosted, args=(sid,), daemon=True).start()
        return session.public()

    def get(self, session_id: str) -> dict[str, Any]:
        session = self._require(session_id)
        self._expire_if_stale(session)
        return session.public()

    def list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for session in self._store.list():
            self._expire_if_stale(session)
            out.append(session.public())
        return out

    def cancel(self, session_id: str) -> dict[str, Any]:
        session = self._require(session_id)
        if session.state != "authenticating":
            return session.public()  # idempotent — already terminal
        self._drop_ticket(session)
        with self._lock:
            proc = self._procs.pop(session.id, None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        session.state = "cancelled"
        self._save(session)
        return session.public()

    def retry(
        self,
        session_id: str,
        *,
        verify: Callable[[str], bool] | None = None,
    ) -> dict[str, Any]:
        session = self._require(session_id)
        self._expire_if_stale(session)
        if session.state == "authenticating":
            raise ConnectError(
                "session_active",
                f"session {session_id!r} is still running — cancel it first",
                status_code=409,
            )
        return self.begin(
            session.provider,
            label=session.label,
            slots=session.slots,
            models=session.models,
            account_id=session.account_id if session.relink else None,
            verify=verify,
        )

    # -- pair lane ----------------------------------------------------------

    def pair_info(self, ticket: str) -> dict[str, Any]:
        """Unauthenticated ticket lookup — the CLI learns the provider."""
        session, _expiry = self._session_for_ticket(ticket, consume=False)
        return {
            "provider": session.provider,
            "account_id": session.account_id,
            "relink": session.relink,
            "session_id": session.id,
            "expires_at": _iso(session.expires_at),
        }

    def complete_pair(
        self,
        ticket: str,
        credential: Any,
        *,
        verify: Callable[[str], bool] | None = None,
    ) -> dict[str, Any]:
        """Consume the ticket and materialize the posted credential blob.

        Validation runs *before* the ticket is consumed — a bad blob is a
        400 the caller can fix and retry, not a spent ticket.
        """
        session, _expiry = self._session_for_ticket(ticket, consume=False)
        try:
            blob = validate_credential_blob(session.provider, credential)
        except OnboardingError as exc:
            raise ConnectError(exc.code, str(exc)) from exc
        if verify is not None:
            self._verify_fns[session.id] = verify
        # Consume the ticket only now — the materialize below owns the
        # session from here on out.
        self._store.pop_ticket(_ticket_hash(ticket))
        self._materialize(session, blob)
        if session.state in ("verified", "materialized"):
            outcome: dict[str, Any] = {
                "account_id": session.account_id,
                "verified": session.state == "verified",
                "connect": session.public(),
            }
            try:
                if session.account_id:
                    outcome["session"] = self._auth.session(session.account_id).to_dict()
            except Exception:
                pass
            return outcome
        raise ConnectError(
            "connect_failed",
            session.error or "pair completion failed",
        )

    # -- internals ----------------------------------------------------------

    def _require(self, session_id: str) -> ConnectSession:
        session = self._store.get(session_id)
        if session is None:
            raise ConnectError(
                "session_not_found",
                f"connect session {session_id!r} not found",
                status_code=404,
            )
        return session

    def _save(self, session: ConnectSession) -> None:
        session.updated_at = _now_iso()
        self._store.put(session)

    def _expire_if_stale(self, session: ConnectSession) -> None:
        if session.state != "authenticating" or session.expires_at >= self._clock():
            return
        self._drop_ticket(session)
        with self._lock:
            proc = self._procs.pop(session.id, None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        session.state = "expired"
        self._save(session)

    def _drop_ticket(self, session: ConnectSession) -> None:
        digest = session.pair_ticket_hash or (
            _ticket_hash(session.pair_ticket) if session.pair_ticket else None
        )
        if digest:
            self._store.pop_ticket(digest)
        session.pair_ticket = None
        session.pair_ticket_hash = None

    def _session_for_ticket(self, ticket: str, *, consume: bool) -> tuple[ConnectSession, float]:
        lookup = self._store.pop_ticket if consume else self._store.get_ticket
        entry = lookup(_ticket_hash(ticket)) if ticket else None
        if entry is None or entry[1] < self._clock():
            raise ConnectError(
                "pair_invalid",
                "pair ticket is invalid, expired, or already used",
                status_code=401,
            )
        session = self._store.get(entry[0])
        if (
            session is None
            or session.kind != "pair"
            or session.state != "authenticating"
            or session.expires_at < self._clock()
        ):
            raise ConnectError(
                "pair_invalid",
                "pair ticket is invalid, expired, or already used",
                status_code=401,
            )
        return session, entry[1]

    def _verify(self, session: ConnectSession, account_id: str) -> bool:
        fn = self._verify_fns.pop(session.id, None)
        if fn is None:
            try:
                return bool(self._auth.verify(account_id).get("verified"))
            except Exception:
                return False
        try:
            return bool(fn(account_id))
        except Exception:
            return False

    def _fail(self, session: ConnectSession, code: str, detail: str) -> None:
        if session.state != "authenticating":
            return
        session.state = "failed"
        session.error = f"{code}: {detail[:200]}"
        self._save(session)

    # -- hosted lane ---------------------------------------------------------

    def _run_hosted(self, session_id: str) -> None:
        session = self._store.get(session_id)
        if session is None or session.state != "authenticating":
            return
        home = self._work_dir / f"home-{session_id}"
        home.mkdir(parents=True, exist_ok=True)
        home.chmod(0o700)
        try:
            argv = self._auth.adapter(session.provider).login_argv(env=self._env)
            if argv is None:
                self._fail(session, "no_login_flow", f"{session.provider} has no login flow")
                return
            if self._launcher is not None:
                rc = self._launcher(
                    argv,
                    host_cli_env(session.provider, home, self._env),
                    lambda line: self._note_output(session_id, line),
                )
            else:
                rc = self._popen_login(argv, session, home)
            fresh = self._store.get(session_id)
            if fresh is None or fresh.state != "authenticating":
                return  # cancelled/expired while the login ran
            if rc != 0:
                self._fail(fresh, "login_failed", f"{session.provider} login exited {rc}")
                return
            self._materialize(fresh, home)
        except OnboardingError as exc:
            self._fail(session, exc.code, str(exc))
        except Exception as exc:
            self._fail(session, "connect_failed", str(exc))

    def _popen_login(self, argv: list[str], session: ConnectSession, home: Path) -> int:
        env = host_cli_env(session.provider, home, self._env)
        try:
            proc = subprocess.Popen(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            raise OnboardingError("cli_missing", f"cannot run {argv[0]!r}: {exc}") from exc
        with self._lock:
            self._procs[session.id] = proc
        try:
            for line in proc.stdout or ():
                self._note_output(session.id, line)
            return int(proc.wait())
        finally:
            with self._lock:
                self._procs.pop(session.id, None)

    def _note_output(self, session_id: str, line: str) -> None:
        session = self._store.get(session_id)
        if session is None or session.state != "authenticating":
            return
        changed = False
        if session.browser_url is None:
            match = _URL_RE.search(line)
            if match:
                session.browser_url = match.group(0).rstrip(".,;)'\"]")
                changed = True
        if session.user_code is None and _CODE_LINE_RE.search(line):
            match = _CODE_RE.search(line)
            if match:
                session.user_code = match.group(1)
                changed = True
        if changed:
            self._save(session)

    # -- materialization (shared hosted/pair) --------------------------------

    def _materialize(self, session: ConnectSession, source: Any) -> None:
        """Capture-or-validate → import/relink → verify → terminal state."""
        blob_path = self._work_dir / f"blob-{session.id}.json"
        try:
            if isinstance(source, dict):
                blob = validate_credential_blob(session.provider, source)
            else:
                blob = self._auth.capture(session.provider, home=Path(source))
            _write_blob_file(blob_path, blob)
            if session.relink and session.account_id:
                outcome = self._auth.relink(session.account_id, source=blob_path, verify=False)
            else:
                outcome = self._auth.import_existing(
                    session.provider,
                    source=blob_path,
                    label=session.label,
                    slots=session.slots,
                    models=session.models,
                    verify=False,
                )
            account_id = str(outcome["account_id"])
            session.account_id = account_id
            session.state = "verified" if self._verify(session, account_id) else "materialized"
            self._save(session)
        except OnboardingError as exc:
            self._fail(session, exc.code, str(exc))
        finally:
            try:
                blob_path.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "CONNECT_SESSION_TTL_S",
    "CONNECT_STATES",
    "ConnectError",
    "ConnectSession",
    "ConnectSessionStore",
    "FileConnectStore",
    "InMemoryConnectStore",
    "ModalDictConnectStore",
    "ProviderConnectService",
    "VERIFY_PURPOSE_TAG",
    "default_connect_service",
    "plane_verify",
    "probe_account_credential",
    "session_from_dict",
    "session_to_dict",
]

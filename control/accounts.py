"""Persistent account registry (SOR-63/D1).

Generalizes the WP0 in-memory ``AccountRegistry`` fakes into a real port
implementation over a pluggable ``AccountStore``:

* ``InMemoryAccountStore`` — tests / ephemeral deployments.
* ``FileAccountStore`` — local durability under a state directory; account
  records and credential blobs are separate JSON files written atomically,
  credential files ``0600``.
* ``ModalDictAccountStore`` — production ``modal.Dict sbx-accounts``; account
  records under ``account/<id>`` keys, credential blobs under
  ``credential/<id>`` keys. (Materializing blobs as Modal Secrets —
  ``sbx-acct-<id>`` — is the SessionService/deploy lane's concern; the
  registry only stores and returns the opaque blob.)

``PersistentAccountRegistry`` implements the frozen
``control.ports.AccountRegistry`` Protocol on top of any store: status
transitions, ``last_used_at``, and ``running_count`` reporting. Running
counts are never persisted (design v2 §3.3: derive from live sessions, do
not store a counter); the registry reports an injected ``running`` source —
normally bound to the ``AccountScheduler`` that holds the slots — and falls
back to a local counter for standalone use.

Credential material stays opaque: the registry stores and returns blobs but
never inspects or logs their contents.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from control.config import ACCOUNTS_DICT_NAME, account_secret_prefix, env_str
from control.ports import Account

ACCOUNT_STATUSES = ("active", "cooling", "invalid", "disabled")

_BLOB_PROVIDER = "provider"
_BLOB_FILES = "files"

# Shared account_id rule (SOR-105): every store maps an id onto a filesystem
# path (``accounts/<id>.json`` / ``credentials/<id>.json``), a ``modal.Dict``
# key (``account/<id>`` / ``credential/<id>``), and the ``sbx-acct-<id>``
# Secret name — only unreserved filename characters are safe, so ``../x``,
# absolute paths, slashes/backslashes, empty and overlong ids are refused
# before any filesystem/Secret/Dict access.
ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def is_valid_account_id(account_id: Any) -> bool:
    """Whether ``account_id`` is safe to use as a store/Secret key."""
    return isinstance(account_id, str) and ACCOUNT_ID_RE.fullmatch(account_id) is not None


def validate_account_id(account_id: Any) -> str:
    """Fail-closed account_id check shared by every store/registry/CLI path.

    Raises ``ValueError`` for anything outside ``ACCOUNT_ID_RE`` — callers
    must run this before the id reaches a filesystem path, ``modal.Dict``
    key, or Secret name.
    """
    if not is_valid_account_id(account_id):
        raise ValueError(
            f"invalid account id {account_id!r}: use 1-128 chars of "
            "[A-Za-z0-9._-], starting with an alphanumeric"
        )
    return account_id


def iso_utc(ts: datetime) -> str:
    """ISO-8601 with tz; the registry persists string timestamps."""
    return ts.isoformat()


def parse_iso(value: str | None) -> datetime | None:
    """Parse a stored ISO-8601 timestamp; naive values are read as UTC."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def cooldown_expired(account: Account, now: datetime) -> bool:
    """A ``cooling`` account whose ``cooldown_until`` has passed."""
    if account.status != "cooling":
        return False
    until = parse_iso(account.cooldown_until)
    return until is not None and until <= now


def account_to_dict(account: Account) -> dict[str, Any]:
    data = asdict(account)
    data["models"] = list(account.models)
    return data


def account_from_dict(raw: Any) -> Account:
    """Decode a stored account record. Raises ``ValueError`` when unusable."""
    if not isinstance(raw, dict):
        raise ValueError("account record is not a dict")
    account_id = raw.get("id")
    provider = raw.get("provider")
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("account record missing id")
    if not is_valid_account_id(account_id):
        raise ValueError(f"account record has unsafe id {account_id!r}")
    if not isinstance(provider, str) or not provider:
        raise ValueError("account record missing provider")
    status = raw.get("status", "active")
    if status not in ACCOUNT_STATUSES:
        raise ValueError(f"account record has unknown status {status!r}")
    models = raw.get("models") or ()
    if not isinstance(models, (list, tuple)) or any(not isinstance(m, str) for m in models):
        raise ValueError("account record field models must be a string list")
    max_concurrent = raw.get("max_concurrent", 1)
    if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int):
        raise ValueError("account record field max_concurrent must be an int")
    extras: dict[str, str | None] = {}
    for key in ("last_used_at", "cooldown_until", "last_error"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"account record field {key} must be a string")
        extras[key] = value
    return Account(
        id=account_id,
        provider=provider,
        label=str(raw.get("label") or ""),
        status=str(status),
        max_concurrent=max_concurrent,
        secret_name=str(raw.get("secret_name") or ""),
        models=tuple(models),
        created_at=str(raw.get("created_at") or ""),
        last_used_at=extras["last_used_at"],
        cooldown_until=extras["cooldown_until"],
        last_error=extras["last_error"],
    )


def corrupt_account(account_id: str, detail: str) -> Account:
    """Synthesized ``disabled`` record for an undecodable stored payload.

    A corrupt record must never be scheduled, but it stays visible in
    ``list()`` so the damage surfaces instead of silently disappearing.
    """
    return Account(
        id=account_id,
        provider="",
        label="",
        status="disabled",
        last_error="corrupt_record",
    )


def _decode(raw: Any, account_id: str) -> Account:
    try:
        account = account_from_dict(raw)
        # The store key is authoritative (SOR-105): a record whose body id
        # differs is corrupt — it must never hand a smuggled id to a lane
        # that re-uses ``account.id`` (running_count, touch, mark_status).
        if account.id != account_id:
            raise ValueError("account record id does not match store key")
        return account
    except (ValueError, TypeError) as exc:
        return corrupt_account(account_id, str(exc))


@runtime_checkable
class AccountStore(Protocol):
    """Raw persistence for account records and credential blobs."""

    def get_record(self, account_id: str) -> dict[str, Any] | None:
        """Stored account payload, or None."""

    def put_record(self, account_id: str, record: dict[str, Any]) -> None:
        """Insert or replace an account payload."""

    def iter_records(self) -> Iterable[tuple[str, Any]]:
        """All ``(account_id, payload)`` pairs, including undecodable ones."""

    def delete_record(self, account_id: str) -> None:
        """Remove the account payload (absent is a no-op)."""

    def get_blob(self, account_id: str) -> dict[str, Any] | None:
        """The credential blob ``{"provider": ..., "files": {...}}`` or None."""

    def put_blob(self, account_id: str, blob: dict[str, Any]) -> None:
        """Store/replace the credential blob. Secret material."""

    def delete_blob(self, account_id: str) -> None:
        """Remove the credential blob (absent is a no-op)."""


class InMemoryAccountStore:
    """Thread-safe dict store for tests and ephemeral deployments."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._blobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get_record(self, account_id: str) -> dict[str, Any] | None:
        validate_account_id(account_id)
        with self._lock:
            raw = self._records.get(account_id)
            return dict(raw) if isinstance(raw, dict) else raw

    def put_record(self, account_id: str, record: dict[str, Any]) -> None:
        validate_account_id(account_id)
        with self._lock:
            self._records[account_id] = dict(record)

    def iter_records(self) -> Iterable[tuple[str, Any]]:
        with self._lock:
            return list(self._records.items())

    def delete_record(self, account_id: str) -> None:
        validate_account_id(account_id)
        with self._lock:
            self._records.pop(account_id, None)

    def get_blob(self, account_id: str) -> dict[str, Any] | None:
        validate_account_id(account_id)
        with self._lock:
            blob = self._blobs.get(account_id)
            return dict(blob) if blob is not None else None

    def put_blob(self, account_id: str, blob: dict[str, Any]) -> None:
        validate_account_id(account_id)
        with self._lock:
            self._blobs[account_id] = dict(blob)

    def delete_blob(self, account_id: str) -> None:
        validate_account_id(account_id)
        with self._lock:
            self._blobs.pop(account_id, None)


class FileAccountStore:
    """Local durable store: JSON files under ``root``.

    Layout::

        root/accounts/<id>.json      account record
        root/credentials/<id>.json   credential blob (mode 0600)

    Writes are atomic (tmp + rename); credential files are created ``0600``.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    def _record_path(self, account_id: str) -> Path:
        # SOR-105: the id is the filename — refuse traversal/absolute/segment
        # ids before any path is formed, even for direct (non-registry) use.
        validate_account_id(account_id)
        return self._root / "accounts" / f"{account_id}.json"

    def _blob_path(self, account_id: str) -> Path:
        validate_account_id(account_id)
        return self._root / "credentials" / f"{account_id}.json"

    @staticmethod
    def _write(path: Path, payload: dict[str, Any], *, secret: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        data = json.dumps(payload, ensure_ascii=False) + "\n"
        if secret:
            # Create 0600 at open — no window where the blob file is
            # umask-readable between write and chmod.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
        else:
            tmp.write_text(data, encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _read(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def get_record(self, account_id: str) -> dict[str, Any] | None:
        raw = self._read(self._record_path(account_id))
        return raw if isinstance(raw, dict) else None

    def put_record(self, account_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._write(self._record_path(account_id), record, secret=False)

    def iter_records(self) -> Iterable[tuple[str, Any]]:
        directory = self._root / "accounts"
        try:
            entries = sorted(directory.glob("*.json"))
        except OSError:
            return []
        out: list[tuple[str, Any]] = []
        for path in entries:
            raw = self._read(path)
            out.append((path.stem, raw if raw is not None else {"id": path.stem}))
        return out

    def delete_record(self, account_id: str) -> None:
        with self._lock:
            self._record_path(account_id).unlink(missing_ok=True)

    def get_blob(self, account_id: str) -> dict[str, Any] | None:
        raw = self._read(self._blob_path(account_id))
        return raw if isinstance(raw, dict) else None

    def put_blob(self, account_id: str, blob: dict[str, Any]) -> None:
        with self._lock:
            self._write(self._blob_path(account_id), blob, secret=True)

    def delete_blob(self, account_id: str) -> None:
        with self._lock:
            self._blob_path(account_id).unlink(missing_ok=True)


class ModalDictAccountStore:
    """Production store backed by ``modal.Dict sbx-accounts``.

    Records live under ``account/<id>`` keys and credential blobs under
    ``credential/<id>`` keys in one Dict. Lazy-imports modal.
    """

    _ACCOUNT_PREFIX = "account/"
    _BLOB_PREFIX = "credential/"

    def __init__(self, name: str = ACCOUNTS_DICT_NAME) -> None:
        self._name = name
        self._dict: Any = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def get_record(self, account_id: str) -> dict[str, Any] | None:
        validate_account_id(account_id)
        raw = self._d().get(self._ACCOUNT_PREFIX + account_id)
        return raw if isinstance(raw, dict) else None

    def put_record(self, account_id: str, record: dict[str, Any]) -> None:
        validate_account_id(account_id)
        self._d().put(self._ACCOUNT_PREFIX + account_id, dict(record))

    def iter_records(self) -> Iterable[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        items: Iterator[tuple[Any, Any]] = self._d().items()
        for key, raw in items:
            if isinstance(key, str) and key.startswith(self._ACCOUNT_PREFIX):
                out.append((key[len(self._ACCOUNT_PREFIX) :], raw))
        return out

    def delete_record(self, account_id: str) -> None:
        validate_account_id(account_id)
        try:
            self._d().pop(self._ACCOUNT_PREFIX + account_id)
        except KeyError:
            return

    def get_blob(self, account_id: str) -> dict[str, Any] | None:
        validate_account_id(account_id)
        raw = self._d().get(self._BLOB_PREFIX + account_id)
        return raw if isinstance(raw, dict) else None

    def put_blob(self, account_id: str, blob: dict[str, Any]) -> None:
        validate_account_id(account_id)
        self._d().put(self._BLOB_PREFIX + account_id, dict(blob))

    def delete_blob(self, account_id: str) -> None:
        validate_account_id(account_id)
        try:
            self._d().pop(self._BLOB_PREFIX + account_id)
        except KeyError:
            return


class PersistentAccountRegistry:
    """``ports.AccountRegistry`` over an ``AccountStore``.

    ``running`` is an optional ``account_id -> live session count`` source
    (design v2 §3.3: running counts derive from sessions, never persisted).
    ``bind_running`` installs it — ``AccountScheduler`` binds itself so the
    counts it grants under its atomic lock are the same ones ``running_count``
    reports. Without a source, the internal counter (``set_running`` /
    ``adjust_running``) answers — enough for standalone/tests.
    """

    def __init__(
        self,
        store: AccountStore,
        *,
        running: Callable[[str], int] | None = None,
    ) -> None:
        self._store = store
        self._running_src = running
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def store(self) -> AccountStore:
        return self._store

    def bind_running(self, source: Callable[[str], int]) -> None:
        """Use ``source`` for ``running_count`` (replaces the local counter)."""
        self._running_src = source

    def set_running(self, account_id: str, count: int) -> None:
        """Test/standalone helper: pretend ``count`` sessions are live."""
        validate_account_id(account_id)
        with self._lock:
            self._counts[account_id] = count

    def adjust_running(self, account_id: str, delta: int) -> None:
        validate_account_id(account_id)
        with self._lock:
            self._counts[account_id] = max(0, self._counts.get(account_id, 0) + delta)

    def list(self, provider: str | None = None) -> list[Account]:
        out = [
            _decode(raw, account_id)
            for account_id, raw in self._store.iter_records()
            if isinstance(account_id, str)
        ]
        if provider is not None:
            out = [a for a in out if a.provider == provider]
        return sorted(out, key=lambda a: a.id)

    def get(self, account_id: str) -> Account | None:
        validate_account_id(account_id)
        raw = self._store.get_record(account_id)
        if raw is None:
            return None
        return _decode(raw, account_id)

    def put(self, account: Account) -> None:
        validate_account_id(account.id)
        if not account.provider:
            raise ValueError("account provider must be non-empty")
        if account.status not in ACCOUNT_STATUSES:
            raise ValueError(f"unknown account status {account.status!r}")
        self._store.put_record(account.id, account_to_dict(account))

    def mark_status(
        self,
        account_id: str,
        status: str,
        *,
        cooldown_until: str | None = None,
        last_error: str | None = None,
    ) -> Account:
        validate_account_id(account_id)
        if status not in ACCOUNT_STATUSES:
            raise ValueError(f"unknown account status {status!r}")
        with self._lock:
            raw = self._store.get_record(account_id)
            if raw is None:
                raise KeyError(account_id)
            updated = replace(
                _decode(raw, account_id),
                status=status,
                cooldown_until=cooldown_until,
                last_error=last_error,
            )
            self._store.put_record(account_id, account_to_dict(updated))
            return updated

    def touch(self, account_id: str, used_at: str) -> None:
        validate_account_id(account_id)
        with self._lock:
            raw = self._store.get_record(account_id)
            if raw is None:
                raise KeyError(account_id)
            updated = replace(_decode(raw, account_id), last_used_at=used_at)
            self._store.put_record(account_id, account_to_dict(updated))

    def remove(self, account_id: str) -> None:
        validate_account_id(account_id)
        self._store.delete_record(account_id)
        self._store.delete_blob(account_id)
        with self._lock:
            self._counts.pop(account_id, None)

    def running_count(self, account_id: str) -> int:
        validate_account_id(account_id)
        if self._running_src is not None:
            return self._running_src(account_id)
        with self._lock:
            return self._counts.get(account_id, 0)

    def get_credential_blob(self, account_id: str) -> dict[str, Any] | None:
        """Return the stored blob; secret material — never log it."""
        validate_account_id(account_id)
        return self._store.get_blob(account_id)

    def put_credential_blob(self, account_id: str, blob: dict[str, Any]) -> None:
        validate_account_id(account_id)
        if not isinstance(blob, dict) or not isinstance(blob.get(_BLOB_FILES), dict):
            raise ValueError("credential blob must be {'provider': P, 'files': {relpath: content}}")
        account = self.get(account_id)
        if account is not None:
            provider = blob.get(_BLOB_PROVIDER)
            if provider is not None and provider != account.provider:
                raise ValueError(
                    f"credential blob provider {provider!r} does not match "
                    f"account {account.provider!r}"
                )
        self._store.put_blob(account_id, blob)


def select_store(
    *,
    store_dir: Path | str | None = None,
    backend: str | None = None,
) -> AccountStore:
    """Pick the account store the same way the rest of the plane does.

    ``SBX_BACKEND=modal`` (or ``backend="modal"``) → the ``sbx-accounts``
    Dict; otherwise a file store under ``store_dir`` /
    ``$SBX_ACCOUNT_STORE_DIR`` / ``$XDG_STATE_HOME/sbx-browser/accounts``.
    """
    kind = backend if backend is not None else os.environ.get("SBX_BACKEND", "local")
    if kind == "modal":
        return ModalDictAccountStore(env_str("SBX_ACCOUNTS_DICT", ACCOUNTS_DICT_NAME))
    root = store_dir or os.environ.get("SBX_ACCOUNT_STORE_DIR")
    if not root:
        xdg = os.environ.get("XDG_STATE_HOME")
        root = Path(xdg) if xdg else Path.home() / ".local" / "state"
        root = Path(root) / "sbx-browser" / "accounts"
    return FileAccountStore(root)


# --------------------------------------------------------------------- CLI


def _pack_blob(provider: str, source: Path) -> dict[str, Any]:
    """Build a credential blob from a file or directory (no logging)."""
    from runtime.runner.adapter import get_adapter

    try:
        credential_files = tuple(get_adapter(provider).credential_files)
    except Exception:
        credential_files = ()
    if source.is_dir():
        if not credential_files:
            raise SystemExit(
                f"--from {source}: directory import needs adapter credential_files "
                f"for provider {provider!r}"
            )
        files: dict[str, str] = {}
        missing: list[str] = []
        for rel in credential_files:
            for candidate in (source / rel, source / Path(rel).name):
                if candidate.is_file():
                    files[rel] = candidate.read_text(encoding="utf-8")
                    break
            else:
                missing.append(rel)
        if missing:
            raise SystemExit(f"--from {source}: missing credential file(s): {', '.join(missing)}")
        return {_BLOB_PROVIDER: provider, _BLOB_FILES: files}
    if not source.is_file():
        raise SystemExit(f"--from {source}: not a file or directory")
    if len(credential_files) > 1:
        raise SystemExit(
            f"--from {source}: provider {provider!r} needs {len(credential_files)} "
            "credential files; pass the containing directory"
        )
    rel = credential_files[0] if credential_files else source.name
    return {_BLOB_PROVIDER: provider, _BLOB_FILES: {rel: source.read_text(encoding="utf-8")}}


def main(argv: list[str] | None = None) -> int:
    """``python -m control.accounts`` — offline account administration.

    Runs against the file store (default) or the Modal Dict (``--modal`` /
    ``SBX_BACKEND=modal``). Never prints credential material.
    """
    parser = argparse.ArgumentParser(prog="control.accounts")
    parser.add_argument("--store-dir", default=None, help="file store root (local mode)")
    parser.add_argument(
        "--modal", action="store_true", help="use the modal.Dict store (SBX_BACKEND=modal)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_import = sub.add_parser("import", help="import a credential blob as a new account")
    p_import.add_argument("--provider", required=True)
    p_import.add_argument("--label", default="")
    p_import.add_argument("--from", dest="source", required=True, help="credential file or dir")
    p_import.add_argument("--account-id", default=None)
    p_import.add_argument("--slots", type=int, default=1, help="max_concurrent")

    p_list = sub.add_parser("list", help="list accounts (no credential material)")
    p_list.add_argument("--provider", default=None)

    for name in ("disable", "enable", "remove"):
        p = sub.add_parser(name)
        p.add_argument("account_id")

    args = parser.parse_args(argv)
    backend = "modal" if args.modal else os.environ.get("SBX_BACKEND", "local")
    registry = PersistentAccountRegistry(select_store(store_dir=args.store_dir, backend=backend))

    try:
        if args.cmd == "import":
            # The id becomes a store path / Secret name — refuse an unsafe id
            # before the credential source or the store is touched (SOR-105).
            account_id = validate_account_id(args.account_id or f"acct_{uuid.uuid4().hex[:8]}")
            blob = _pack_blob(args.provider, Path(args.source).expanduser())
            account = Account(
                id=account_id,
                provider=args.provider,
                label=args.label or account_id,
                status="active",
                max_concurrent=args.slots,
                secret_name=f"{account_secret_prefix()}{account_id}",
                created_at=datetime.now(UTC).isoformat(),
            )
            registry.put(account)
            registry.put_credential_blob(account_id, blob)
            print(f"imported {account_id} provider={args.provider} files={len(blob[_BLOB_FILES])}")
            return 0
        if args.cmd == "list":
            for account in registry.list(args.provider):
                # A corrupt record keeps its (possibly unsafe) store key as
                # id — never feed it back into running_count (SOR-105).
                running = (
                    registry.running_count(account.id) if is_valid_account_id(account.id) else 0
                )
                print(
                    f"{account.id}\t{account.provider}\t{account.status}\t"
                    f"running={running}/{account.max_concurrent}\t"
                    f"{account.label}"
                )
            return 0
        if args.cmd == "disable":
            registry.mark_status(validate_account_id(args.account_id), "disabled")
            print(f"disabled {args.account_id}")
            return 0
        if args.cmd == "enable":
            registry.mark_status(validate_account_id(args.account_id), "active")
            print(f"enabled {args.account_id}")
            return 0
        if args.cmd == "remove":
            registry.remove(validate_account_id(args.account_id))
            print(f"removed {args.account_id}")
            return 0
        return 2
    except ValueError as exc:
        # Never a traceback for a refused id — and the message carries only
        # the id itself, never credential material.
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

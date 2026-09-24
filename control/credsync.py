"""SOR-147 (R011 WP-H1): OAuth credential auto-refresh write-back.

Provider CLIs rotate OAuth tokens inside the sandbox (``codex`` rewrites
``~/.codex/auth.json`` etc.). This module harvests those refreshed files back
to the control plane: after each turn (and once more at close) it execs
``runner export-credentials`` in the session sandbox and, when the blob
changed, commits it to the account store and refreshes the account's
deployment-managed Modal Secret in place — no redeploy. The official CLI
auth files stay authoritative: the committed blob is exactly what the CLI
wrote, validated through the same ``onboarding.validate_credential_blob``
schema an import uses.

Safety shape:

* **Stale-writer protection** — commit is compare-and-swap on a sha256
  fingerprint: the session records the fingerprint of the blob it was
  seeded with (``cred_base_fp`` tag) and the commit only lands while the
  stored blob still equals it. A concurrent writer (another session of the
  same account, a manual ``onboarding refresh``) makes this writer stale
  and its export is dropped — a slow sandbox can never clobber a newer
  credential with the older one it captured.
* **Short commit locking** — a per-account lock guards only the commit
  section (re-read → compare → store → Secret refresh). The export exec
  itself runs outside any lock.
* **One-shot ``auth_invalid`` self-heal** — an ``auth_invalid`` turn still
  runs the export once; if the CLI rotated its token the new blob commits
  and an ``invalid`` account reactivates. The /v1 failure reporter treats
  the run's ``auth_invalid`` verdict as stale when the stored credential
  already rotated past the fingerprint the failing run was seeded with
  (``cred_run_fp``), so a healed account is not re-demoted by old evidence.
* **Zero leakage** — the export stdout is consumed in-process only; blob
  material is never logged, never written to events or the run ledger, and
  only re-enters a sandbox through ``SBX_ACCOUNT_CREDENTIAL``.

Residual: ``modal.Dict`` has no CAS primitive, so the fingerprint check is
best-effort across control-plane replicas — within a process the per-account
lock serializes writers; across replicas two commits could both pass the
compare. The credential store stays the authority either way.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from control.accounts import is_valid_account_id
from control.config import account_secret_prefix, env_str
from control.sandbox_io import sandbox_env

# Session ``sandbox_tags`` keys (SOR-147). ``cred_base_fp`` is the fingerprint
# of the credential blob the session's sandbox was seeded with — the CAS
# comparand every commit from this session is checked against. ``cred_run_fp``
# is the fingerprint of the credential material a specific dispatched run ran
# with — the /v1 failure reporter compares it to the stored blob so an
# ``auth_invalid`` verdict computed against a since-rotated credential never
# re-marks a healed account.
TAG_CRED_BASE_FP = "cred_base_fp"
TAG_CRED_RUN_FP = "cred_run_fp"

CREDENTIAL_ENV = "SBX_ACCOUNT_CREDENTIAL"
_EXPORT_BYTES_LIMIT = 512 * 1024


def blob_fingerprint(blob: Mapping[str, Any] | None) -> str | None:
    """Content fingerprint of a credential blob — order-insensitive sha256."""
    if not isinstance(blob, Mapping):
        return None
    canonical = json.dumps(blob, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def credential_rotated(registry: Any, account_id: str, run_fp: str | None) -> bool:
    """True when the stored blob's fingerprint differs from ``run_fp``.

    The verdict a run produced was computed against the credential it was
    seeded with; a mismatch means the credential has since rotated (a
    write-back/self-heal commit, a manual refresh), so the verdict is stale.
    """
    if not run_fp or not account_id or account_id == "auto":
        return False
    get_blob = getattr(registry, "get_credential_blob", None)
    if not callable(get_blob):
        return False
    try:
        stored = get_blob(account_id)
    except Exception:
        return False
    return stored is not None and blob_fingerprint(stored) != run_fp


class CredentialSecretWriter(Protocol):
    """Refresh a named Modal Secret in place. Never logs ``env`` values."""

    def refresh(self, secret_name: str, env: dict[str, str]) -> None: ...


class ModalCredentialSecretWriter:
    """Recreate a named Modal Secret (Secrets are immutable: delete + create)."""

    def refresh(self, secret_name: str, env: dict[str, str]) -> None:
        import modal

        objects = modal.Secret.objects
        objects.delete(secret_name, allow_missing=True)
        objects.create(secret_name, env_dict=env)


@dataclass(frozen=True)
class WritebackOutcome:
    """Result of one write-back attempt.

    ``code``: ``committed`` | ``unchanged`` | ``stale`` | ``invalid`` |
    ``skipped:<reason>``. ``fingerprint`` is the committed blob's fingerprint
    (callers fold it into ``cred_base_fp``). ``healed`` marks a commit that
    reactivated an ``invalid`` account (the one-shot auth_invalid self-heal).
    ``secret``: ``refreshed`` | ``skipped`` | ``failed`` — a failed Secret
    refresh never undoes the store commit, which stays authoritative.
    """

    code: str
    fingerprint: str | None = None
    healed: bool = False
    secret: str = "skipped"


class CredentialSync:
    """Harvest refreshed credential files from session sandboxes.

    ``registry_source`` is a callable returning the account registry (or
    ``None`` when no account lane is installed) — resolved lazily per call so
    the sync is inert until bootstrap/tests install a registry. Local
    backends get the store write-back; Modal deployments additionally pass a
    :class:`CredentialSecretWriter` so the managed ``<prefix><id>`` Secret is
    recreated in place without a redeploy.
    """

    def __init__(
        self,
        registry_source: Callable[[], Any | None],
        *,
        secret_writer: CredentialSecretWriter | None = None,
        lifecycle: Any = None,
    ) -> None:
        self._registry_source = registry_source
        self._secret_writer = secret_writer
        self._lifecycle = lifecycle
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    @staticmethod
    def enabled() -> bool:
        """Kill-switch: ``SBX_CRED_WRITEBACK=0`` disables all write-back."""
        return env_str("SBX_CRED_WRITEBACK", "1") != "0"

    def seed_fingerprint(self, account_id: str | None) -> str | None:
        """Fingerprint of the blob a session will be seeded with.

        Recorded as ``cred_base_fp`` at provision so the session's later
        commits can prove they still see the same stored credential.
        """
        registry = self._registry()
        if registry is None or not self._usable(account_id):
            return None
        try:
            return blob_fingerprint(registry.get_credential_blob(account_id))
        except Exception:
            return None

    @staticmethod
    def mark_run_credential(tags: dict[str, str]) -> None:
        """Copy ``cred_base_fp`` → ``cred_run_fp`` for the run being dispatched."""
        base = tags.get(TAG_CRED_BASE_FP)
        if base:
            tags[TAG_CRED_RUN_FP] = base

    def writeback(
        self,
        *,
        backend: Any,
        handle: Any,
        runner_cmd: list[str],
        tags: Mapping[str, str] | None,
    ) -> WritebackOutcome:
        """Export the sandbox's credential files and commit a changed blob.

        Best-effort by contract: the caller wraps this in try/except; every
        non-commit outcome is reported via ``WritebackOutcome.code`` rather
        than raised. ``auth_invalid`` turns are handled by the same path —
        a commit on that path is the one-shot self-heal.
        """
        if not self.enabled():
            return WritebackOutcome("skipped:disabled")
        registry = self._registry()
        if registry is None:
            return WritebackOutcome("skipped:no_registry")
        tags = tags or {}
        account_id = tags.get("account_id")
        if not self._usable(account_id):
            return WritebackOutcome("skipped:account")
        base_fp = tags.get(TAG_CRED_BASE_FP)
        if base_fp is None:
            # Session provisioned before SOR-147 — no CAS comparand exists,
            # so committing is unsafe: a stored rotation since provision
            # would be clobbered. Next provision seeds the anchor.
            return WritebackOutcome("skipped:no_base")
        try:
            account = registry.get(account_id)
        except Exception:
            return WritebackOutcome("skipped:registry_error")
        if account is None:
            return WritebackOutcome("skipped:account")

        # SOR-176: static credentials (OpenCode Zen ``api`` entries, Devin
        # keys) have no refresh channel — never spend an export exec on them.
        try:
            from control.credlifecycle import credential_kind

            stored_blob = registry.get_credential_blob(account_id)
        except Exception:
            stored_blob = None
        if credential_kind(getattr(account, "provider", ""), stored_blob) == "api_key":
            return WritebackOutcome("skipped:static_credential")

        exported = self._export_blob(backend, handle, runner_cmd)
        if exported is None:
            return WritebackOutcome("unchanged")
        if blob_fingerprint(exported) == base_fp:
            return WritebackOutcome("unchanged")
        try:
            from control.onboarding import validate_credential_blob

            validate_credential_blob(account.provider, exported)
        except Exception:
            return WritebackOutcome("invalid")

        # Short commit lock: re-read, compare, store, refresh the Secret —
        # serialized per account so local writers can't interleave the CAS.
        with self._lock_for(account_id):
            try:
                stored = registry.get_credential_blob(account_id)
            except Exception:
                return WritebackOutcome("skipped:registry_error")
            stored_fp = blob_fingerprint(stored)
            if stored_fp is None:
                return WritebackOutcome("skipped:no_stored_blob")
            if stored_fp != base_fp:
                # The stored credential moved since this session was seeded —
                # committing would clobber the newer material.
                return WritebackOutcome("stale")
            exported_fp = blob_fingerprint(exported)
            if exported_fp == stored_fp:
                return WritebackOutcome("unchanged")
            try:
                registry.put_credential_blob(account_id, exported)
            except Exception:
                return WritebackOutcome("skipped:registry_error")
            secret_state = self._refresh_secret(account_id, account, exported)
            healed = False
            try:
                current = registry.get(account_id)
                if current is not None and current.status == "invalid":
                    registry.mark_status(account_id, "active", last_error=None)
                    healed = True
            except Exception:
                pass
        outcome = WritebackOutcome(
            "committed", fingerprint=exported_fp, healed=healed, secret=secret_state
        )
        self._note_commit(account_id, exported)
        return outcome

    def _note_commit(self, account_id: str, blob: dict) -> None:
        """SOR-176: feed the committed rotation into the lifecycle record."""
        service = self._lifecycle_service()
        if service is None:
            return
        try:
            service.finish_refresh(account_id, outcome="committed", blob=blob)
        except Exception:
            pass

    def _lifecycle_service(self) -> Any | None:
        if self._lifecycle is not None:
            return self._lifecycle
        try:
            from control.credlifecycle import CredentialLifecycleService

            self._lifecycle = CredentialLifecycleService(self._registry_source)
        except Exception:
            self._lifecycle = None
        return self._lifecycle

    # ------------------------------------------------------------ internals

    def _registry(self) -> Any | None:
        try:
            return self._registry_source()
        except Exception:
            return None

    @staticmethod
    def _usable(account_id: Any) -> bool:
        return (
            isinstance(account_id, str) and account_id != "auto" and is_valid_account_id(account_id)
        )

    def _lock_for(self, account_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(account_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[account_id] = lock
            return lock

    def _export_blob(self, backend: Any, handle: Any, runner_cmd: list[str]) -> dict | None:
        """Run ``runner export-credentials``; return the printed blob or None.

        The runner prints only when the restored files differ from the
        injected ``SBX_ACCOUNT_CREDENTIAL`` blob — an exec failure, non-zero
        exit, empty output, or unparseable payload all mean "no write-back".
        stdout is consumed here only and never reaches logs or events.
        """
        try:
            proc = backend.exec(
                handle,
                [*runner_cmd, "export-credentials"],
                env=sandbox_env(handle),
            )
        except Exception:
            return None
        size = 0
        chunks: list[str] = []
        overflow = False
        try:
            for line in proc.stdout:
                size += len(line)
                if size > _EXPORT_BYTES_LIMIT:
                    overflow = True
                    break
                chunks.append(line)
        except Exception:
            return None
        try:
            code = proc.wait()
        except Exception:
            return None
        if overflow or code != 0:
            return None
        text = "".join(chunks).strip()
        if not text:
            return None
        try:
            blob = json.loads(text)
        except ValueError:
            return None
        if not isinstance(blob, dict) or not isinstance(blob.get("files"), dict):
            return None
        return blob

    def _refresh_secret(self, account_id: str, account: Any, blob: dict) -> str:
        """Recreate the deployment-managed ``<prefix><id>`` Secret in place.

        Custom ``secret_name`` accounts are skipped: only names this
        deployment manages may be replaced — those are materialized from the
        credential lane at deploy time, so refreshing just the lane leaves
        them consistent.
        """
        writer = self._secret_writer
        managed = f"{account_secret_prefix()}{account_id}"
        if writer is None or getattr(account, "secret_name", None) != managed:
            return "skipped"
        try:
            writer.refresh(
                managed,
                {CREDENTIAL_ENV: json.dumps(blob, ensure_ascii=False, separators=(",", ":"))},
            )
        except Exception:
            return "failed"
        return "refreshed"

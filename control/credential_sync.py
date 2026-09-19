"""Runtime OAuth credential write-back (SOR-147 / WP-H1).

Provider CLIs refresh their own auth files inside the sandbox (the official
CLI auth files stay authoritative). The control plane's job is to carry a
refreshed credential back out:

* **export** — ``ControlPlane`` runs ``runner export-credentials`` after each
  finished turn and on ``close()``; the runner re-reads the restored files
  and prints a new ``{"provider", "files"}`` blob only when they changed.
* **commit** — ``CredentialSync.commit`` validates the blob against the
  provider descriptor and compare-and-swaps it into the registry under a
  short per-account lock: the write lands only while the stored blob still
  matches the digest the session mounted (``base_sha256`` captured at
  provision). A refreshed Secret remounted mid-turn makes stale sandbox
  files *look* like an export — the digest gate drops that older generation
  instead of rolling the store back (stale-writer protection).
* **publish** — a committed blob is pushed to the account's Modal Secret at
  runtime, so the next sandbox exec (which re-resolves ``Secret.from_name``)
  and every later session get the refresh without a redeploy. Only the
  deployment-managed ``<account_secret_prefix><account_id>`` name is written;
  a custom or empty ``secret_name`` is externally managed and never touched.
* **self-heal** — ``auth_invalid`` verdicts from sessions that mounted a
  since-superseded credential are ignored, and an account parked
  ``invalid``/``auth_invalid`` is unparked by the first committed refresh
  (one-shot: an unchanged commit is a no-op, so nothing can loop).

Secret material never enters logs or run records — only digests are kept on
the in-memory session meta.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from control.accounts import (
    COMMIT_COMMITTED,
    COMMIT_STALE,
    COMMIT_UNCHANGED,
    credential_blob_digest,
    is_valid_account_id,
)
from control.config import account_secret_prefix
from control.onboarding import validate_credential_blob

CREDENTIAL_ENV = "SBX_ACCOUNT_CREDENTIAL"

# Commit outcomes beyond the registry's COMMIT_* values.
COMMIT_REFUSED = "refused"  # malformed blob, unknown account, validation/store error
COMMIT_NO_REGISTRY = "no_registry"  # no account registry is wired

_COMMIT_OUTCOMES = (COMMIT_COMMITTED, COMMIT_UNCHANGED, COMMIT_STALE)


def modal_secret_writer(secret_name: str, env: dict[str, str]) -> None:
    """Replace a named Modal Secret in place (delete + create — no overwrite).

    Same ordering ``sbx/deploy.py`` uses to materialize account Secrets.
    ``modal`` is imported lazily so local/test planes never touch the client.
    """
    import modal

    modal.Secret.objects.delete(secret_name, allow_missing=True)
    modal.Secret.objects.create(secret_name, env_dict=env)


class CredentialSync:
    """Best-effort credential export → registry CAS → Secret publish lane.

    ``registry_source`` is an ``AccountRegistry``-shaped object or a callable
    returning one (or ``None`` when the plane has no accounts configured).
    ``secret_writer(secret_name, env)`` publishes a committed blob to the
    named Secret; pass ``modal_secret_writer`` on the Modal backend and leave
    it ``None`` locally.
    """

    def __init__(
        self,
        registry_source: Any,
        *,
        secret_writer: Callable[[str, dict[str, str]], None] | None = None,
    ) -> None:
        self._registry_source = registry_source
        self._secret_writer = secret_writer
        # Short refresh-commit locks: serialize read-compare-write + publish
        # per account without serializing unrelated accounts.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _registry(self) -> Any | None:
        source = self._registry_source
        return source() if callable(source) else source

    def _lock_for(self, account_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(account_id)
            if lock is None:
                lock = self._locks[account_id] = threading.Lock()
            return lock

    def base_digest(self, account_id: str) -> str | None:
        """Digest of the stored blob — the generation a session mounts.

        ``None`` when the account has no stored blob or the digest cannot be
        read; commit CAS treats ``None`` as "store must still be empty".
        """
        registry = self._registry()
        if registry is None or not is_valid_account_id(account_id):
            return None
        try:
            return credential_blob_digest(registry.get_credential_blob(account_id))
        except Exception:
            return None

    def credential_superseded(self, account_id: str, base_sha256: str | None) -> bool:
        """Stored blob moved past the generation ``base_sha256`` pins."""
        registry = self._registry()
        if registry is None or not is_valid_account_id(account_id):
            return False
        try:
            current = registry.get_credential_blob(account_id)
        except Exception:
            return False
        return credential_blob_digest(current) != base_sha256

    def commit(
        self,
        account_id: str,
        blob: Any,
        *,
        base_sha256: str | None,
    ) -> str:
        """Validate then CAS-commit an exported credential blob.

        Returns ``committed`` / ``unchanged`` / ``stale`` / ``refused`` /
        ``no_registry``. On ``committed`` the managed account Secret is
        republished (when a writer is wired) and an ``auth_invalid``-parked
        account is one-shot healed back to ``active``. Never raises and never
        logs credential material.
        """
        if not is_valid_account_id(account_id) or not isinstance(blob, dict):
            return COMMIT_REFUSED
        registry = self._registry()
        if registry is None:
            return COMMIT_NO_REGISTRY
        try:
            account = registry.get(account_id)
        except Exception:
            return COMMIT_REFUSED
        if account is None:
            return COMMIT_REFUSED
        try:
            # require_full: an export is a whole credential set — a partial
            # write would produce a mixed-generation blob.
            validate_credential_blob(account.provider, blob, require_full=True)
        except Exception:
            return COMMIT_REFUSED
        with self._lock_for(account_id):
            try:
                outcome = self._cas(registry, account_id, blob, base_sha256)
            except Exception:
                return COMMIT_REFUSED
            if outcome != COMMIT_COMMITTED:
                return outcome
            # Publish inside the per-account lock so registry write and Secret
            # refresh stay one atomic commit; a failed publish keeps the
            # registry truth and is retried by the next commit.
            try:
                self._publish_secret(account, blob)
            except Exception:
                pass
            self._heal_auth_invalid(account_id)
            return COMMIT_COMMITTED

    def skip_auth_invalid_report(self, account_id: str, base_sha256: str | None) -> bool:
        """Whether an ``auth_invalid`` failure report should be dropped.

        When the stored credential already moved past the generation the
        failing session mounted, the verdict targets stale material: the
        account is unparked (one-shot heal) and the failure must NOT mark it
        ``invalid`` again. Returns ``True`` when the report is superseded.
        """
        if not self.credential_superseded(account_id, base_sha256):
            return False
        self._heal_auth_invalid(account_id)
        return True

    def _cas(self, registry: Any, account_id: str, blob: dict, base_sha256: str | None) -> str:
        commit_fn = getattr(registry, "commit_credential_blob", None)
        if commit_fn is not None:
            return commit_fn(account_id, blob, base_sha256=base_sha256)
        # Fallback for registries without native CAS — still serialized by
        # the per-account lock held in commit().
        current = registry.get_credential_blob(account_id)
        if current == blob:
            return COMMIT_UNCHANGED
        if credential_blob_digest(current) != base_sha256:
            return COMMIT_STALE
        registry.put_credential_blob(account_id, blob)
        return COMMIT_COMMITTED

    def _publish_secret(self, account: Any, blob: dict) -> None:
        if self._secret_writer is None:
            return
        name = getattr(account, "secret_name", None)
        if name != f"{account_secret_prefix()}{account.id}":
            # Empty/custom secret names are externally managed (deploy only
            # materializes ``<prefix><id>``) — never overwrite them.
            return
        self._secret_writer(name, {CREDENTIAL_ENV: json.dumps(blob)})

    def _heal_auth_invalid(self, account_id: str) -> None:
        """Unpark ``invalid`` + ``auth_invalid`` once a fresher blob exists."""
        try:
            registry = self._registry()
            account = registry.get(account_id) if registry is not None else None
            if (
                account is not None
                and account.status == "invalid"
                and account.last_error == "auth_invalid"
            ):
                registry.mark_status(account_id, "active", last_error=None)
        except Exception:
            pass


def parse_exported_blob(lines: list[str]) -> dict[str, Any] | None:
    """Last non-empty stdout line of ``runner export-credentials`` as a blob.

    The runner prints the blob as its single output line; earlier event lines
    or a partially consumed stream are ignored, and anything unparseable is a
    no-export result — never a crash, never logged.
    """
    for line in reversed(lines):
        raw = line.strip()
        if not raw:
            continue
        try:
            blob = json.loads(raw)
        except (json.JSONDecodeError, RecursionError):
            return None
        return blob if isinstance(blob, dict) else None
    return None

"""P2 control-plane ports: account registry, scheduler, API key store.

Frozen Protocol shells and data classes (WP0 / SOR-59). P2-C (SOR-63)
implements them against ``modal.Dict`` + per-account Modal Secrets; P2-D
(SOR-64) and the internal ``/api/*`` routes consume them. Credential material
is only ever exchanged as the blob ``{"provider": P, "files": {relpath:
content}}`` (see ``docs/contracts/runner-cli.md``) and must never be logged.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

ProviderId = Literal["codex", "antigravity", "grok", "opencode", "devin"]

AccountStatus = Literal["active", "cooling", "invalid", "disabled"]

ApiKeyScope = Literal["agents", "admin"]

# Scheduler decision error codes (canonical error_subcodes; api.yaml).
SCHEDULE_ERRORS = (
    "account_busy",
    "account_unavailable",
    "provider_exhausted",
    "invalid_provider",
)


@dataclass(frozen=True)
class Account:
    """One provider account. Stored in ``modal.Dict sbx-accounts`` keyed by id.

    Mirrors the JSON shape in design v2 §3.3. ``last_error`` is a short
    REDACTED code only — never credential material.
    """

    id: str
    provider: str
    label: str
    status: str = "active"  # AccountStatus
    max_concurrent: int = 1
    secret_name: str = ""
    models: tuple[str, ...] = ()
    created_at: str = ""  # ISO-8601
    last_used_at: str | None = None
    cooldown_until: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class ApiKey:
    """An API key record. Only the sha256 of the ``sbx_<key>`` token is kept."""

    id: str
    key_hash: str
    label: str = ""
    scopes: tuple[str, ...] = ("agents",)
    created_at: str = ""  # ISO-8601
    revoked_at: str | None = None


@dataclass(frozen=True)
class ScheduleDecision:
    """Outcome of ``Scheduler.decide``.

    ``account`` set → proceed with that account. Otherwise ``error`` is one of
    ``SCHEDULE_ERRORS``; ``retry_after`` (seconds) may accompany
    ``provider_exhausted``.
    """

    account: Account | None = None
    error: str | None = None
    retry_after: float | None = None


@runtime_checkable
class AccountRegistry(Protocol):
    """Provider accounts and their credential blobs."""

    def list(self, provider: str | None = None) -> list[Account]:
        """All accounts, optionally filtered by provider."""

    def get(self, account_id: str) -> Account | None:
        """Return the account or None."""

    def put(self, account: Account) -> None:
        """Insert or replace an account record."""

    def mark_status(
        self,
        account_id: str,
        status: str,
        *,
        cooldown_until: str | None = None,
        last_error: str | None = None,
    ) -> Account:
        """Transition status (active/cooling/invalid/disabled); KeyError if missing."""

    def touch(self, account_id: str, used_at: str) -> None:
        """Record ``last_used_at`` (ISO-8601)."""

    def remove(self, account_id: str) -> None:
        """Delete the account record and its credential blob."""

    def running_count(self, account_id: str) -> int:
        """Live sessions on this account (derived from the sessions store)."""

    def get_credential_blob(self, account_id: str) -> dict[str, Any] | None:
        """Return ``{"provider": ..., "files": {relpath: content}}`` or None.

        Secret material — callers must not log or persist it elsewhere.
        """

    def put_credential_blob(self, account_id: str, blob: dict[str, Any]) -> None:
        """Store/replace the credential blob (import or ``export-credentials`` write-back)."""


@runtime_checkable
class Scheduler(Protocol):
    """Pick an account for a new session (design v2 §3.3)."""

    def decide(self, *, provider: str, account: str | None = "auto") -> ScheduleDecision:
        """Resolve ``account`` (an id or ``"auto"``/None) to a ScheduleDecision.

        Named account: ``account_unavailable`` if not ``active`` or unknown,
        ``account_busy`` if ``running >= max_concurrent``. ``auto``: LRU pick
        among ``active`` accounts with a free slot, else
        ``provider_exhausted``. Unknown provider → ``invalid_provider``.
        """


@runtime_checkable
class SessionService(Protocol):
    """Session lifecycle surface consumed by the public ``/v1`` API (P2-D).

    Implemented by ``control.service.ControlPlane``. P2-D calls only the
    methods listed here; ``provider`` / ``account_id`` on ``create_session``
    are added by P2-C (SOR-63) — v1 callers omit them and get ``codex`` /
    ``"auto"`` defaults.
    """

    def create_session(
        self,
        *,
        owner: str,
        title: str | None,
        provider: str = "codex",
        account_id: str = "auto",
        model: str | None = None,
    ) -> str:
        """Create a session (one sandbox); returns the session id."""

    def get(self, session_id: str) -> Any:
        """Return the SessionRecord or None."""

    def list_sessions(self) -> list[dict[str, Any]]:
        """Public-shaped session dicts (see api.yaml Session schema)."""

    def post_message(self, session_id: str, text: str) -> str:
        """Start a turn; returns the turn id. Raises ControlError on conflict."""

    def stop(self, session_id: str) -> str:
        """Stop the current turn; returns the new status."""

    def close(self, session_id: str) -> Any:
        """Close the session; returns the updated SessionRecord."""

    def public(self, rec: Any) -> dict[str, Any]:
        """Serialize a SessionRecord to the api.yaml Session shape."""


@runtime_checkable
class ApiKeyStore(Protocol):
    """Bearer ``sbx_<key>`` API keys; only hashes are stored."""

    def create(self, *, label: str = "", scopes: Iterable[str] = ("agents",)) -> tuple[ApiKey, str]:
        """Generate a key; return ``(record, plaintext)``. Plaintext is shown once."""

    def list(self) -> list[ApiKey]:
        """All keys (records only, never plaintext)."""

    def lookup(self, token: str) -> ApiKey | None:
        """Resolve a Bearer token to its record; None if unknown or revoked."""

    def revoke(self, key_id: str) -> bool:
        """Revoke by id; False if unknown."""

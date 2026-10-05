"""The command authority transaction — dedupe, order, commit (RFC 167 §04).

A command is: begin a Unit of Work → enforce the caller's dedupe key → run
the handler (which mutates typed projections, appends events, enqueues
deduped Jobs/outbox) → record the dedupe response → commit. A replayed
command returns the stored response without re-running the handler; a
reused key with a changed body is a hard idempotency_conflict.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from control.domain import ids
from control.domain.errors import DomainError
from control.persistence.database import Database
from control.persistence.unit_of_work import SqlUnitOfWork


def canonical_request_digest(payload: dict) -> str:
    """Canonical digest of the dedupe-scoped request body."""
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


@dataclass
class Replay:
    """A dedupe hit — the stored first response, replayed verbatim."""

    response: dict


class IdempotencyConflict(DomainError):
    def __init__(self, key: str) -> None:
        super().__init__(
            "idempotency_conflict",
            f"dedupe key {key!r} was reused with a different request body",
            details={"key": key},
        )


class CommandBus:
    """Runs commands under the single authority transaction."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def run(
        self,
        *,
        principal: dict,
        workspace_id: str,
        command_kind: str,
        dedupe_key: str | None,
        request_digest: str,
        handler: Callable[[SqlUnitOfWork], dict],
    ) -> dict | Replay:
        """One authority transaction. ``dedupe_key=None`` skips dedupe."""
        with SqlUnitOfWork(self.db, actor=principal) as uow:
            record = None
            if dedupe_key is not None:
                status, stored = uow.dedupe.begin(
                    dedupe_id=ids.new_id("command_dedup"),
                    principal_id=principal.get("id", "system"),
                    workspace_id=workspace_id,
                    command_kind=command_kind,
                    key=dedupe_key,
                    request_digest=request_digest,
                )
                if status == "conflict":
                    raise IdempotencyConflict(dedupe_key)
                if status == "replay":
                    record = Replay(stored or {})
            if record is None:
                result = handler(uow)
                if dedupe_key is not None:
                    uow.dedupe.record_response(
                        principal_id=principal.get("id", "system"),
                        workspace_id=workspace_id,
                        command_kind=command_kind,
                        key=dedupe_key,
                        response=result,
                    )
            uow.commit()
        return record if record is not None else result

"""Committed journal append — the one writer path for Session events.

Sequence is allocated off the locked Session row (`next_event_seq`); the
UNIQUE(session_id, seq) constraint is the durable backstop. Runtime-source
appends additionally dedupe on (lease, epoch, local_seq).
"""

from __future__ import annotations

from control.domain import ids
from control.domain.events import ENVELOPE_SCHEMA_VERSION, EventSource
from control.persistence.unit_of_work import SqlUnitOfWork


def append_event(
    uow: SqlUnitOfWork,
    *,
    workspace_id: str,
    session_id: str,
    event_type: str,
    source: EventSource | str = EventSource.APPLICATION,
    actor: dict | None = None,
    payload: dict | None = None,
    turn_id: str | None = None,
    execution_id: str | None = None,
    executor_lease_id: str | None = None,
    lease_generation: int | None = None,
    delegation_id: str | None = None,
    changeset_id: str | None = None,
    delivery_id: str | None = None,
    causation_id: str | None = None,
    correlation_id: str | None = None,
    runtime_epoch: str | None = None,
    local_seq: int | None = None,
    adapter_version: str | None = None,
    cli_version: str | None = None,
    observed_at=None,
) -> dict:
    """Allocate seq and append one committed event. The caller's Session row
    must already be locked (FOR UPDATE) — this keeps seq contiguous."""
    src = source.value if isinstance(source, EventSource) else source
    seq = uow.sessions.allocate(workspace_id, session_id, "next_event_seq")
    return uow.events.append(
        {
            "id": ids.new_id("event"),
            "workspace_id": workspace_id,
            "session_id": session_id,
            "seq": seq,
            "type": event_type,
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "actor": actor or uow.actor,
            "source": src,
            "causation_id": causation_id,
            "correlation_id": correlation_id,
            "turn_id": turn_id,
            "execution_id": execution_id,
            "executor_lease_id": executor_lease_id,
            "lease_generation": lease_generation,
            "delegation_id": delegation_id,
            "changeset_id": changeset_id,
            "delivery_id": delivery_id,
            "runtime_epoch": runtime_epoch,
            "local_seq": local_seq,
            "adapter_version": adapter_version,
            "cli_version": cli_version,
            "payload": payload or {},
        }
    )


def ingest_runtime_events(
    uow: SqlUnitOfWork,
    *,
    workspace_id: str,
    session_id: str,
    lease_id: str,
    runtime_epoch: str,
    events: list[dict],
) -> list[dict]:
    """Deduped runtime-source ingest (RFC 167 §04 event model).

    Each spooled record carries (lease, epoch, local_seq). Within the Session
    row lock we check the durable tuple dedupe + the committed-offset ack;
    unacked records append and advance the offset in the same transaction,
    so a replayed spool item is a no-op and an out-of-order one waits for
    the contiguous prefix to commit first.
    """
    committed: list[dict] = []
    offset = uow.ingestion_offsets.get_for_update(lease_id, runtime_epoch)
    acked = offset["committed_local_seq"] if offset else 0
    for rec in sorted(events, key=lambda r: r["local_seq"]):
        lseq = rec["local_seq"]
        if lseq <= acked:
            continue  # already committed — replay no-op
        if uow.events.exists_runtime(lease_id, runtime_epoch, lseq):
            acked = max(acked, lseq)
            continue  # durable tuple dedupe — previously committed
        row = append_event(
            uow,
            workspace_id=workspace_id,
            session_id=session_id,
            event_type=rec["type"],
            source=EventSource.RUNTIME,
            actor=rec.get("actor") or {"kind": "runtime", "id": lease_id},
            payload=rec.get("payload") or {},
            turn_id=rec.get("turn_id"),
            execution_id=rec.get("execution_id"),
            executor_lease_id=lease_id,
            lease_generation=rec.get("lease_generation"),
            delegation_id=rec.get("delegation_id"),
            changeset_id=rec.get("changeset_id"),
            delivery_id=rec.get("delivery_id"),
            causation_id=rec.get("causation_id"),
            correlation_id=rec.get("correlation_id"),
            runtime_epoch=runtime_epoch,
            local_seq=lseq,
            adapter_version=rec.get("adapter_version"),
            cli_version=rec.get("cli_version"),
            observed_at=rec.get("observed_at"),
        )
        committed.append(row)
        acked = max(acked, lseq)
    if committed or offset is None:
        uow.ingestion_offsets.upsert_advance(
            lease_id=lease_id,
            runtime_epoch=runtime_epoch,
            session_id=session_id,
            committed_local_seq=acked,
        )
    return committed

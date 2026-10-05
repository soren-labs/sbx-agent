"""The job worker — claim, run, settle, backoff (RFC 167 §04).

Claim protocol lives in the DB (FOR UPDATE SKIP LOCKED + generation
fencing), so workers are replaceable: any holder can die mid-handler and
the row is reclaimed after its claim expires. ``settle`` requires the
recorded generation — a stale holder's completion is a no-op.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from control.domain.jobs import JobKind
from control.persistence.unit_of_work import SqlUnitOfWork

#: A handler returns a result dict (succeeded), raises JobRetry for a
#: retryable fault, or any other exception for a terminal failure.
JobHandler = Callable[[dict, "JobContext"], dict]


class JobRetry(Exception):
    """Retryable failure — retry_wait with backoff until the limit."""

    def __init__(
        self, message: str, *, retry_after: float | None = None, detail: dict | None = None
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.detail = detail or {}


@dataclass
class JobContext:
    """Per-attempt handler context — renews the claim mid-work if asked."""

    uow: SqlUnitOfWork
    job: dict
    holder: str
    lease_seconds: float

    def renew(self) -> bool:
        return self.uow.jobs.renew(
            job_id=self.job["id"],
            holder=self.holder,
            generation=self.job["claim_generation"],
            lease_seconds=self.lease_seconds,
        )


def backoff_seconds(attempt: int, base: float = 0.5, cap: float = 60.0) -> float:
    """Bounded exponential backoff on the 1-based attempt ordinal."""
    return min(cap, base * (2 ** max(0, attempt - 1)))


@dataclass
class Worker:
    """Claims and settles durable Jobs. ``holder`` is this worker's
    claim-holder identity (unique per worker instance)."""

    db: Any
    holder: str
    handlers: dict = field(default_factory=dict)
    lease_seconds: float = 30.0
    kinds: list | None = None

    def claim(self, limit: int = 1) -> list[dict]:
        """Claim due/expired jobs — one small transaction per batch."""
        with SqlUnitOfWork(self.db, actor={"kind": "worker", "id": self.holder}) as uow:
            rows = uow.jobs.claim_due(
                holder=self.holder,
                lease_seconds=self.lease_seconds,
                kinds=[k.value if isinstance(k, JobKind) else k for k in self.kinds]
                if self.kinds
                else None,
                limit=limit,
            )
            uow.commit()
            return rows

    def settle(
        self,
        job: dict,
        *,
        outcome: str,
        result: dict | None = None,
        error: dict | None = None,
        retry_after: float | None = None,
    ) -> bool:
        """Settle with the recorded generation; false when the fence moved."""
        state = {
            "succeeded": "succeeded",
            "retry": "retry_wait",
            "failed": "failed",
            "cancelled": "cancelled",
        }[outcome]
        with SqlUnitOfWork(self.db, actor={"kind": "worker", "id": self.holder}) as uow:
            row = uow.jobs.complete(
                job_id=job["id"],
                holder=self.holder,
                generation=job["claim_generation"],
                state=state,
                result=result,
                error=error,
                retry_after_seconds=retry_after,
            )
            uow.job_attempts.finish(
                job_id=job["id"],
                ordinal=job["attempts"],
                outcome=outcome,
                error=error,
                evidence={"settled": row is not None},
            )
            uow.commit()
            return row is not None

    def run_job(self, job: dict) -> str:
        """Run one claimed job's handler and settle it. Returns outcome."""
        kind = job["kind"]
        handler = self.handlers.get(kind)
        outcome, result, error, retry_after = "failed", None, None, None
        if handler is None:
            error = {"code": "unimplemented", "message": f"no handler for {kind}"}
        else:
            try:
                with SqlUnitOfWork(self.db, actor={"kind": "worker", "id": self.holder}) as uow:
                    ctx = JobContext(
                        uow=uow,
                        job=job,
                        holder=self.holder,
                        lease_seconds=self.lease_seconds,
                    )
                    result = handler(job, ctx) or {}
                    uow.commit()
                outcome = "succeeded"
            except JobRetry as e:
                outcome = "retry"
                error = {"code": "retryable", "message": str(e), **e.detail}
                retry_after = e.retry_after
                if job["attempts"] >= job["attempt_limit"]:
                    outcome = "failed"
                    error = {**error, "code": "attempt_limit"}
            except Exception as e:  # terminal handler fault
                outcome = "failed"
                error = {
                    "code": "handler_error",
                    "message": f"{type(e).__name__}: {e}",
                    "traceback": traceback.format_exc(limit=8),
                }
                if job["attempts"] >= job["attempt_limit"]:
                    error = {**error, "code": "attempt_limit"}
        self.settle(
            job,
            outcome=outcome,
            result=result,
            error=error,
            retry_after=retry_after or backoff_seconds(job["attempts"]),
        )
        return outcome

    def step(self) -> int:
        """Claim and run up to one due job. Returns jobs processed."""
        jobs = self.claim(limit=1)
        for job in jobs:
            self.run_job(job)
        return len(jobs)

    def run_until_idle(self, max_steps: int = 100, sleep: float = 0.05) -> int:
        """Drain the queue — deterministic test/maintenance driver."""
        total = 0
        for _ in range(max_steps):
            n = self.step()
            total += n
            if n == 0:
                break
            time.sleep(sleep)
        return total


def drain_outbox(db, *, limit: int = 50, actor: dict | None = None) -> int:
    """Deliver due outbox messages. Local deliveries (session:/principal:
    destinations) settle in-DB; webhook destinations settle the same row —
    the record is the durable evidence of the effect either way."""
    with SqlUnitOfWork(db, actor=actor or {"kind": "system"}) as uow:
        rows = uow.outbox.due(limit=limit)
        for row in rows:
            uow.outbox.mark(row["workspace_id"], row["id"], "delivered")
        uow.commit()
        return len(rows)

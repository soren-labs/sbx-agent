"""Transactional claim/renew/complete with increasing generation (RFC 04).

Stale worker completion is rejected even when its external call succeeded;
the successor inspects the same effect ID.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from control.domain.errors import DomainError
from control.domain.ids import new_id
from control.jobs.backoff import delay_for
from control.jobs.model import Cancelled, Claim, Continue, Failed, Outcome, Retry, Succeeded


class StaleClaim(DomainError):
    def __init__(self, claim: Claim, why: str) -> None:
        super().__init__(
            "stale_fence",
            f"job claim {claim.job_id} generation {claim.generation} is no longer current: {why}",
            details={"job_id": claim.job_id, "generation": claim.generation},
        )


def claim_next(
    db: Any, holder: str, *, kinds: list[str] | None = None, lease_seconds: float = 60
) -> Claim | None:
    def fn(uow: Any) -> Claim | None:
        row = uow.query_one("jobs.claimable", kinds=kinds)
        if row is None:
            return None
        now = uow.now()
        reclaimed = row["state"] == "claimed"
        if reclaimed:
            uow.update_where(
                "job_attempts",
                {"job_id": row["id"], "generation": row["claim_generation"]},
                {"finished_at": now, "outcome": "claim_expired"},
            )
        generation = row["claim_generation"] + 1
        expires = now + timedelta(seconds=lease_seconds)
        job = uow.update(
            "jobs",
            row["id"],
            {
                "state": "claimed",
                "claim_generation": generation,
                "claim_holder": holder,
                "claim_expires_at": expires,
                "attempts": row["attempts"] + 1,
                "updated_at": now,
            },
        )
        uow.insert(
            "job_attempts",
            {
                "id": new_id("job_attempt"),
                "job_id": row["id"],
                "generation": generation,
                "holder": holder,
                "reclaimed": reclaimed,
            },
        )
        return Claim(
            job_id=job["id"],
            kind=job["kind"],
            generation=generation,
            holder=holder,
            expires_at=expires,
            workspace_id=job["workspace_id"],
            effect_id=job["effect_id"],
            attempts=job["attempts"],
            reclaimed=reclaimed,
            job=job,
        )

    return db.run(fn)


def assert_current(uow: Any, claim: Claim) -> dict[str, Any]:
    row = uow.query_one("jobs.lock_claim", job_id=claim.job_id)
    if row is None:
        raise StaleClaim(claim, "job missing")
    if row["state"] != "claimed":
        raise StaleClaim(claim, f"job is {row['state']}")
    if row["claim_generation"] != claim.generation or row["claim_holder"] != claim.holder:
        raise StaleClaim(claim, "claim superseded")
    if row["claim_expires_at"] <= uow.now():
        raise StaleClaim(claim, "claim expired")
    return row


def renew(db: Any, claim: Claim, *, lease_seconds: float = 60) -> Claim:
    def fn(uow: Any) -> Claim:
        assert_current(uow, claim)
        expires = uow.now() + timedelta(seconds=lease_seconds)
        uow.update("jobs", claim.job_id, {"claim_expires_at": expires})
        return Claim(**{**claim.__dict__, "expires_at": expires})

    return db.run(fn)


def finish(uow: Any, claim: Claim, outcome: Outcome) -> None:
    row = assert_current(uow, claim)
    now = uow.now()
    values: dict[str, Any] = {"updated_at": now, "claim_holder": None, "claim_expires_at": None}
    attempt_outcome = type(outcome).__name__.lower()
    error_code = None
    if isinstance(outcome, Succeeded):
        values.update(state="succeeded", finished_at=now, result=outcome.result or {})
    elif isinstance(outcome, Continue):
        values.update(state="queued", due_at=now + timedelta(seconds=outcome.delay))
        if outcome.input is not None:
            values["input"] = {**(row["input"] or {}), **outcome.input}
    elif isinstance(outcome, Retry):
        error_code = outcome.code
        exhausted = row["attempts"] >= row["max_attempts"]
        past_deadline = row["deadline_at"] is not None and row["deadline_at"] <= now
        values.update(last_error_code=outcome.code, last_error=outcome.message[:500])
        if exhausted or past_deadline:
            values.update(state="failed", finished_at=now)
            attempt_outcome = "exhausted"
        else:
            delay = outcome.delay
            if delay is None:
                delay = delay_for(row["attempts"], retry_after=outcome.retry_after)
            values.update(state="retry_wait", due_at=now + timedelta(seconds=delay))
    elif isinstance(outcome, Failed):
        error_code = outcome.code
        values.update(
            state="failed",
            finished_at=now,
            last_error_code=outcome.code,
            last_error=outcome.message[:500],
        )
    elif isinstance(outcome, Cancelled):
        values.update(state="cancelled", finished_at=now, last_error=outcome.reason[:500])
    else:  # pragma: no cover
        raise TypeError(outcome)
    uow.update("jobs", claim.job_id, values)
    uow.update_where(
        "job_attempts",
        {"job_id": claim.job_id, "generation": claim.generation},
        {"finished_at": now, "outcome": attempt_outcome, "error_code": error_code},
    )


def cancel_active(uow: Any, *, kind: str, dedupe_key: str, reason: str) -> int:
    """Cancel not-yet-claimed Jobs; a claimed one observes domain state itself."""
    return uow.update_where(
        "jobs",
        {"kind": kind, "dedupe_key": dedupe_key, "state": ["queued", "retry_wait"]},
        {"state": "cancelled", "last_error": reason, "finished_at": uow.now()},
    )

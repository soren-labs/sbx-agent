from dataclasses import dataclass

from control.domain.errors import require
from control.domain.identity import new_id


@dataclass(frozen=True)
class Claim:
    job_id: str
    holder: str
    generation: int
    row: dict


class Claims:
    def __init__(self, uow):
        self.uow = uow

    def take(self, holder: str, seconds=120):
        with self.uow.transaction() as repo:
            job = repo.one(
                "SELECT * FROM jobs WHERE ((state IN ('queued','retry_wait') AND due_at<=now()) "
                "OR (state='claimed' AND claim_expires_at<now())) AND deadline>now() "
                "ORDER BY priority DESC,due_at,id FOR UPDATE SKIP LOCKED LIMIT 1"
            )
            if not job:
                return None
            row = repo.one(
                "UPDATE jobs SET state='claimed',holder=%s,claim_generation=claim_generation+1,"
                "claim_expires_at=now()+%s*interval '1 second',attempts=attempts+1 "
                "WHERE id=%s RETURNING *",
                (holder, seconds, job["id"]),
            )
            repo.execute(
                "INSERT INTO job_attempts(id,workspace_id,job_id,generation,holder) "
                "VALUES(%s,%s,%s,%s,%s)",
                (
                    new_id("attempt"),
                    row["workspace_id"],
                    row["id"],
                    row["claim_generation"],
                    holder,
                ),
            )
            return Claim(row["id"], holder, row["claim_generation"], row)

    def assert_current(self, repo, claim):
        row = repo.one("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (claim.job_id,))
        require(
            row
            and row["state"] == "claimed"
            and row["holder"] == claim.holder
            and row["claim_generation"] == claim.generation,
            "version_conflict",
        )
        require(
            repo.one(
                "SELECT claim_expires_at>now() AS valid FROM jobs WHERE id=%s", (claim.job_id,)
            )["valid"],
            "version_conflict",
        )
        return row

    def finish(self, claim, state="succeeded", error=None, delay=0):
        with self.uow.transaction() as repo:
            self.assert_current(repo, claim)
            repo.execute(
                "UPDATE jobs SET state=%s,last_error=%s,due_at=now()+%s*interval '1 second',"
                "holder=NULL,claim_expires_at=NULL WHERE id=%s",
                (state, error, delay, claim.job_id),
            )

    def renew(self, claim, seconds=120):
        with self.uow.transaction() as repo:
            self.assert_current(repo, claim)
            repo.execute(
                "UPDATE jobs SET claim_expires_at=now()+%s*interval '1 second' WHERE id=%s",
                (seconds, claim.job_id),
            )

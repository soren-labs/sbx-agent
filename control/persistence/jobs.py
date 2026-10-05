"""Job + JobAttempt repositories — the durable claim protocol (RFC 167 §04)."""

from __future__ import annotations

from control.domain.jobs import TARGET_FK_COLUMN, TargetFamily

from .base import Rows, _now, sqlexpr


class JobRepo(Rows):
    def insert(self, row: dict) -> dict:
        """Insert with the target FK placed in its typed column."""
        row = dict(row)
        family = TargetFamily(row["target_family"])
        fk = TARGET_FK_COLUMN[family]
        if fk and row.get("target_id"):
            row[fk] = row.pop("target_id")
        else:
            row.pop("target_id", None)
        return self.insert_row("jobs", row)

    def target_id(self, row: dict) -> str | None:
        family = TargetFamily(row["target_family"])
        fk = TARGET_FK_COLUMN[family]
        return row.get(fk) if fk else None

    def get(self, workspace_id: str, job_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM jobs WHERE workspace_id=%s AND id=%s",
            (workspace_id, job_id),
        )

    def get_for_update(self, workspace_id: str, job_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM jobs WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (workspace_id, job_id),
        )

    def find_active_by_dedupe(self, dedupe_key: str) -> dict | None:
        return self.one(
            "SELECT * FROM jobs WHERE dedupe_key=%s AND state IN ('queued','claimed','retry_wait')",
            (dedupe_key,),
        )

    def find_by_effect(self, effect_id: str) -> dict | None:
        return self.one("SELECT * FROM jobs WHERE effect_id=%s", (effect_id,))

    def claim_due(
        self,
        *,
        holder: str,
        lease_seconds: float,
        kinds: list[str] | None = None,
        limit: int = 1,
    ) -> list[dict]:
        """Claim due queued/retry rows AND reclaim expired claimed rows.

        One atomic statement: claim increments generation, stamps the holder
        and expiry, and inserts a JobAttempt. FOR UPDATE SKIP LOCKED makes
        concurrent workers disjoint.
        """
        sql = """
        WITH candidates AS (
            SELECT id FROM jobs
            WHERE (
                (state IN ('queued','retry_wait') AND due_at <= now())
                OR (state = 'claimed' AND claim_expires_at < now())
            )
            {kind_filter}
            ORDER BY priority, due_at
            LIMIT %(lim)s
            FOR UPDATE SKIP LOCKED
        ),
        claimed AS (
            UPDATE jobs j SET
                state = 'claimed',
                claim_generation = j.claim_generation + 1,
                claim_holder = %(holder)s,
                claim_expires_at = now() + (%(lease)s || ' seconds')::interval,
                attempts = j.attempts + 1,
                updated_at = now()
            FROM candidates c
            WHERE j.id = c.id
            RETURNING j.*
        ),
        attempt_rows AS (
            INSERT INTO job_attempts (id, workspace_id, job_id, ordinal, holder,
                                      generation)
            SELECT %(attpfx)s || md5(random()::text || clock_timestamp()::text),
                   c.workspace_id, c.id, c.attempts, %(holder)s, c.claim_generation
            FROM claimed c
            RETURNING job_id
        )
        SELECT * FROM claimed
        """
        kind_filter = ""
        params = {"holder": holder, "lease": lease_seconds, "lim": limit, "attpfx": "jatt_"}
        if kinds:
            kind_filter = "AND kind = ANY(%(kinds)s)"
            params["kinds"] = kinds
        return self.all(sql.replace("{kind_filter}", kind_filter), params)

    def renew(self, *, job_id: str, holder: str, generation: int, lease_seconds: float) -> bool:
        row = self.one(
            "UPDATE jobs SET claim_expires_at = now() + (%s || ' seconds')::interval,"
            " updated_at = now()"
            " WHERE id=%s AND state='claimed' AND claim_holder=%s"
            " AND claim_generation=%s AND claim_expires_at > now() - interval '5 minutes'"
            " RETURNING id",
            (lease_seconds, job_id, holder, generation),
        )
        return row is not None

    def complete(
        self,
        *,
        job_id: str,
        holder: str,
        generation: int,
        state: str,
        result: dict | None = None,
        error: dict | None = None,
        retry_after_seconds: float | None = None,
    ) -> dict | None:
        """Finish a claim; stale generations are rejected by the WHERE clause."""
        changes: dict = {
            "state": state,
            "result": result,
            "last_error": error,
            "updated_at": _now(),
            "claim_holder": None,
            "claim_expires_at": None,
        }
        if state == "retry_wait":
            changes["due_at"] = sqlexpr(
                f"now() + ({int(retry_after_seconds or 5)} || ' seconds')::interval"
            )
        return self.update_row(
            "jobs",
            {
                "id": job_id,
                "state": "claimed",
                "claim_holder": holder,
                "claim_generation": generation,
            },
            changes,
            version_column=None,
        )

    def update(self, workspace_id: str, job_id: str, changes: dict) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "jobs",
            {"workspace_id": workspace_id, "id": job_id},
            changes,
            version_column=None,
        )

    def list_by_target(self, workspace_id: str, family: str, target_id: str) -> list[dict]:
        fk = TARGET_FK_COLUMN[TargetFamily(family)]
        if not fk:
            return []
        return self.all(
            f"SELECT * FROM jobs WHERE workspace_id=%s AND {fk}=%s ORDER BY created_at",
            (workspace_id, target_id),
        )


class JobAttemptRepo(Rows):
    def finish(
        self,
        *,
        job_id: str,
        ordinal: int,
        outcome: str,
        error: dict | None = None,
        evidence: dict | None = None,
    ) -> dict | None:
        return self.update_row(
            "job_attempts",
            {"job_id": job_id, "ordinal": ordinal},
            {
                "finished_at": _now(),
                "outcome": outcome,
                "error": error,
                "evidence": evidence or {},
            },
            version_column=None,
        )

    def list_by_job(self, job_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM job_attempts WHERE job_id=%s ORDER BY ordinal",
            (job_id,),
        )

"""Delivery, DeliveryStep, target claims and MergeRequest repositories."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from .base import Rows, _now


class DeliveryRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("deliveries", row)

    def get(self, workspace_id: str, delivery_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM deliveries WHERE workspace_id=%s AND id=%s",
            (workspace_id, delivery_id),
        )

    def get_for_update(self, workspace_id: str, delivery_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM deliveries WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (workspace_id, delivery_id),
        )

    def get_by_changeset(self, workspace_id: str, changeset_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM deliveries WHERE workspace_id=%s AND changeset_id=%s"
            " ORDER BY created_at DESC LIMIT 1",
            (workspace_id, changeset_id),
        )

    def update(
        self,
        workspace_id: str,
        delivery_id: str,
        changes: dict,
        *,
        expected_version: int | None = None,
    ) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "deliveries",
            {"workspace_id": workspace_id, "id": delivery_id},
            changes,
            expected_version=expected_version,
        )

    def list_by_session(self, workspace_id: str, session_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM deliveries WHERE workspace_id=%s AND session_id=%s ORDER BY created_at",
            (workspace_id, session_id),
        )


class DeliveryStepRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("delivery_steps", row)

    def upsert_step(
        self,
        *,
        delivery_id: str,
        workspace_id: str,
        kind: str,
        effect_id: str,
        state: str,
        expected: dict | None = None,
        result: dict | None = None,
    ) -> dict:
        """Idempotent step record: (delivery, kind, effect_id) is the unique
        gate. A re-run of the same effect updates the same row."""
        return self.one_required(
            "INSERT INTO delivery_steps (id, workspace_id, delivery_id,"
            " ordinal, kind, effect_id, expected, result, state)"
            " VALUES (%(i)s, %(w)s, %(d)s,"
            " (SELECT COALESCE(MAX(ordinal),0)+1 FROM delivery_steps"
            "  WHERE delivery_id=%(d)s), %(k)s, %(e)s, %(x)s, %(r)s, %(s)s)"
            " ON CONFLICT (delivery_id, kind, effect_id) DO UPDATE SET"
            " state=%(s)s, result=%(r)s, observed_at=now()"
            " RETURNING *",
            {
                "i": _new_step_id(),
                "w": workspace_id,
                "d": delivery_id,
                "k": kind,
                "e": effect_id,
                "x": Jsonb(expected or {}),
                "r": Jsonb(result or {}),
                "s": state,
            },
        )

    def list_for(self, workspace_id: str, delivery_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM delivery_steps WHERE workspace_id=%s"
            " AND delivery_id=%s ORDER BY ordinal",
            (workspace_id, delivery_id),
        )


class DeliveryTargetClaimRepo(Rows):
    """Exact-subject delivery locks: one row per (workspace, repository,
    ref_or_pr) fencing the target a Delivery intends to write. The holder is
    the owning delivery/merge id; generation bumps on every takeover."""

    def claim(
        self,
        *,
        claim_id: str,
        workspace_id: str,
        holder: str,
        repository: str,
        ref_or_pr: str,
        expires_at=None,
    ) -> dict | None:
        """Take or extend the fence; returns the row iff held by ``holder``."""
        row = self.one(
            "INSERT INTO delivery_target_claims (id, workspace_id, repository,"
            " ref_or_pr, generation, holder, expires_at)"
            " VALUES (%(i)s, %(w)s, %(r)s, %(f)s, 1, %(h)s, %(e)s)"
            " ON CONFLICT (workspace_id, repository, ref_or_pr) DO NOTHING"
            " RETURNING *",
            {
                "i": claim_id,
                "w": workspace_id,
                "r": repository,
                "f": ref_or_pr,
                "h": holder,
                "e": expires_at,
            },
        )
        if row is not None:
            return row
        # Contend an existing fence: take over only if holder matches or expired.
        return self.one(
            "UPDATE delivery_target_claims SET generation = generation + 1,"
            " holder=%(h)s, expires_at=%(e)s"
            " WHERE workspace_id=%(w)s AND repository=%(r)s AND ref_or_pr=%(f)s"
            " AND (holder=%(h)s OR (expires_at IS NOT NULL AND expires_at < now()))"
            " RETURNING *",
            {
                "w": workspace_id,
                "r": repository,
                "f": ref_or_pr,
                "h": holder,
                "e": expires_at,
            },
        )

    def release(self, workspace_id: str, claim_id: str, holder: str) -> None:
        self.conn.execute(
            "DELETE FROM delivery_target_claims WHERE workspace_id=%s AND id=%s AND holder=%s",
            (workspace_id, claim_id, holder),
        )


class MergeRequestRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("merge_requests", row)

    def get(self, workspace_id: str, merge_request_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM merge_requests WHERE workspace_id=%s AND id=%s",
            (workspace_id, merge_request_id),
        )

    def get_for_update(self, workspace_id: str, merge_request_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM merge_requests WHERE workspace_id=%s AND id=%s FOR UPDATE",
            (workspace_id, merge_request_id),
        )

    def active_for_delivery(self, workspace_id: str, delivery_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM merge_requests WHERE workspace_id=%s AND delivery_id=%s"
            " AND state IN ('pending','executing','blocked')",
            (workspace_id, delivery_id),
        )

    def update(self, workspace_id: str, merge_request_id: str, changes: dict) -> dict | None:
        changes = dict(changes)
        changes["updated_at"] = _now()
        return self.update_row(
            "merge_requests",
            {"workspace_id": workspace_id, "id": merge_request_id},
            changes,
            version_column=None,
        )


def _new_step_id() -> str:
    from control.domain import ids

    return ids.new_id("delivery_step")

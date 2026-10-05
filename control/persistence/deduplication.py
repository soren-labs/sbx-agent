"""Command deduplication — request/replay records (RFC 167 §04)."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from .base import Rows


class DedupeRepo(Rows):
    """Dedupe keys are scoped (principal_id, workspace_id, command_kind, key)."""

    def begin(
        self,
        *,
        dedupe_id: str,
        principal_id: str,
        workspace_id: str,
        command_kind: str,
        key: str,
        request_digest: str,
    ) -> tuple[str, dict | None]:
        """Try to reserve the key.

        Returns ('new', None) when this caller owns the key,
        ('replay', stored_response) for a same-body replay, or
        ('conflict', None) for a changed body under a reused key.
        """
        row = self.one(
            "INSERT INTO command_deduplication (id, principal_id, workspace_id,"
            " command_kind, key, request_digest, response)"
            " VALUES (%(i)s, %(p)s, %(w)s, %(k)s, %(key)s, %(d)s, '{}')"
            " ON CONFLICT (principal_id, workspace_id, command_kind, key)"
            " DO NOTHING RETURNING id",
            {
                "i": dedupe_id,
                "p": principal_id,
                "w": workspace_id,
                "k": command_kind,
                "key": key,
                "d": request_digest,
            },
        )
        if row is not None:
            return "new", None
        existing = self.one(
            "SELECT request_digest, response FROM command_deduplication"
            " WHERE principal_id=%s AND workspace_id=%s AND command_kind=%s"
            " AND key=%s",
            (principal_id, workspace_id, command_kind, key),
        )
        if existing and existing["request_digest"] == request_digest:
            return "replay", existing["response"]
        return "conflict", None

    def record_response(
        self,
        *,
        principal_id: str,
        workspace_id: str,
        command_kind: str,
        key: str,
        response: dict,
    ) -> None:
        self.one(
            "UPDATE command_deduplication SET response=%(r)s"
            " WHERE principal_id=%(p)s AND workspace_id=%(w)s"
            " AND command_kind=%(k)s AND key=%(key)s RETURNING id",
            {
                "r": Jsonb(response),
                "p": principal_id,
                "w": workspace_id,
                "k": command_kind,
                "key": key,
            },
        )

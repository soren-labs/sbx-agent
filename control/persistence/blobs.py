"""Content-addressed blob registry and references."""

from __future__ import annotations

from .base import Rows


class BlobRepo(Rows):
    """Blobs are content-addressed by sha256 digest (``blob_<digest>`` ids);
    storage backends hold the bytes. The DB rows are immutable."""

    def insert(self, row: dict) -> dict:
        return self.insert_row("blobs", row)

    def get(self, workspace_id: str, blob_id: str) -> dict | None:
        return self.one(
            "SELECT * FROM blobs WHERE workspace_id=%s AND id=%s",
            (workspace_id, blob_id),
        )

    def get_by_digest(self, workspace_id: str, digest: str) -> dict | None:
        return self.one(
            "SELECT * FROM blobs WHERE workspace_id=%s AND digest=%s",
            (workspace_id, digest),
        )


class BlobReferenceRepo(Rows):
    def insert(self, row: dict) -> dict:
        return self.insert_row("blob_references", row)

    def list_for(self, workspace_id: str, blob_id: str) -> list[dict]:
        return self.all(
            "SELECT * FROM blob_references WHERE workspace_id=%s AND blob_id=%s",
            (workspace_id, blob_id),
        )

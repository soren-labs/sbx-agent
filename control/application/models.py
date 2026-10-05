"""Model discovery: usable models behind the caller's own connections
(RFC 167 §06 — capability discovery is metadata-shaped, cached as a
scoped observation, and prefers a free usable model)."""

from __future__ import annotations

from dataclasses import dataclass

from control.application.connections import (
    PURPOSE_RUNTIME_EXECUTION,
    ConnectionService,
)
from control.connectors.base import ConnectorRegistry
from control.domain.errors import DomainError
from control.persistence.unit_of_work import SqlUnitOfWork

_CATALOG_SCOPE = "model_catalog"
_CATALOG_TTL_S = 3600


@dataclass
class ModelOption:
    id: str  # e.g. "opencode/big-pickle"
    provider_id: str
    free: bool
    usable: bool
    connection_id: str
    health: str


class ModelDiscoveryService:
    def __init__(
        self,
        db,
        connections: ConnectionService,
        connectors: ConnectorRegistry,
    ):
        self.db = db
        self.connections = connections
        self.connectors = connectors

    def list_models(
        self,
        uow: SqlUnitOfWork,
        *,
        workspace_id: str,
        force_refresh: bool = False,
    ) -> list[ModelOption]:
        """Models usable by this workspace's configured inference
        connections. Catalog comes from a scoped observation when fresh."""
        out: list[ModelOption] = []
        for kind in ("opencode_zen", "codex"):
            for conn in self._configured(uow, workspace_id, kind):
                obs = None
                if not force_refresh:
                    obs = self._fresh_catalog(uow, workspace_id, conn)
                if obs is None:
                    obs = self._probe_catalog(uow, workspace_id, conn)
                if obs is None:
                    continue
                for m in obs["result"].get("models", []):
                    out.append(
                        ModelOption(
                            id=m["id"],
                            provider_id=kind,
                            free=bool(m.get("free")),
                            usable=bool(m.get("usable", True)),
                            connection_id=conn["id"],
                            health=conn["health"],
                        )
                    )
        # Prefer free usable first — the MVP default selection.
        out.sort(key=lambda m: (not (m.usable and m.free), not m.usable, m.id))
        return out

    def pick_default(self, uow: SqlUnitOfWork, *, workspace_id: str) -> ModelOption:
        models = self.list_models(uow, workspace_id=workspace_id)
        usable = [m for m in models if m.usable]
        if not usable:
            raise DomainError(
                "executor_unavailable", "no usable models behind configured connections"
            )
        return usable[0]

    def _configured(self, uow, workspace_id: str, kind: str) -> list[dict]:
        return [
            c
            for c in uow.connections.find_by_kind(workspace_id, kind)
            if c["state"] == "configured"
            and PURPOSE_RUNTIME_EXECUTION in (c["allowed_purposes"] or [])
        ]

    def _fresh_catalog(self, uow, workspace_id: str, conn: dict) -> dict | None:
        rows = [
            o
            for o in uow.connection_observations.list_for(workspace_id, conn["id"])
            if o["kind"] == "capability" and o["scope"] == _CATALOG_SCOPE
        ]
        if not rows:
            return None
        latest = rows[-1]
        if latest["expires_at"] and latest["expires_at"] <= _now():
            return None
        return latest

    def _probe_catalog(self, uow, workspace_id: str, conn: dict) -> dict | None:
        connector = self.connectors.get(conn["kind"])
        if not hasattr(connector, "discover_models"):
            return None
        material = self.connections.materialize(
            uow,
            workspace_id=workspace_id,
            connection_id=conn["id"],
            purpose=PURPOSE_RUNTIME_EXECUTION,
        )
        try:
            models = connector.discover_models(material.payload) or []
        finally:
            del material  # plaintext dies with this frame
        obs = self.connections.record_observation(
            uow,
            workspace_id=workspace_id,
            connection_id=conn["id"],
            credential_version_id=conn["current_credential_version_id"],
            kind="capability",
            scope=_CATALOG_SCOPE,
            result={"models": models},
            ttl_s=_CATALOG_TTL_S,
        )
        return obs


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)

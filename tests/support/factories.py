"""Direct fixtures for principals/catalogs used before product auth exists in a test."""

from __future__ import annotations

from typing import Any

from control.application.resolution import ExplicitResolver, StaticCatalog
from control.application.sessions import Sessions
from control.domain.identity import Principal
from control.domain.ids import new_id

TEST_MANIFESTS: list[dict[str, Any]] = [
    {
        "provider_id": "opencode",
        "support_tier": "supported",
        "capabilities": {
            "native_resume": {"status": "supported"},
            "steer": {"status": "unsupported"},
            "effort_settings": {"status": "unsupported"},
            "model_discovery": {"status": "supported"},
        },
    },
    {"provider_id": "claude", "support_tier": "disabled", "capabilities": {}},
]


def catalog() -> StaticCatalog:
    return StaticCatalog(TEST_MANIFESTS)


def make_principal(db: Any, email: str | None = None) -> Principal:
    user_id, workspace_id = new_id("user"), new_id("workspace")

    def fn(uow: Any) -> None:
        uow.insert("users", {"id": user_id, "email": email or f"{user_id}@example.test"})
        uow.insert("workspaces", {"id": workspace_id, "owner_user_id": user_id, "name": "personal"})
        uow.insert(
            "workspace_memberships",
            {"workspace_id": workspace_id, "user_id": user_id, "role": "owner"},
        )

    db.run(fn)
    return Principal(user_id=user_id, workspace_ids=(workspace_id,))


def sessions_app(db: Any) -> Sessions:
    cat = catalog()
    return Sessions(db, ExplicitResolver(cat), cat)


def session_body(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "harness": {"provider_id": "opencode", "model": "opencode/big-pickle"},
        "executor": {"backend": "local"},
        "title": "test session",
    }
    body.update(extra)
    return body

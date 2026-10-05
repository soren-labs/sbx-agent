"""Published OpenAPI matches the single /api surface; no legacy API-version routers (RFC 08)."""

from __future__ import annotations

from scripts.export_openapi import TARGET, render


def test_openapi_published_and_current() -> None:
    assert TARGET.read_text() == render(), "run: uv run python scripts/export_openapi.py"


def test_single_business_surface() -> None:
    import yaml

    paths = yaml.safe_load(render())["paths"]
    assert all(p.startswith(("/api/", "/internal/tools/", "/healthz", "/readyz")) for p in paths)
    assert not any(p.startswith(("/v1", "/v2", "/hosted", "/api/v2")) for p in paths)
    for required in (
        "/api/auth/login",
        "/api/workspaces/{workspace_id}/connections",
        "/api/sessions/{session_id}/events",
        "/api/changesets/{changeset_id}/deliveries",
        "/api/deliveries/{delivery_id}/merge-requests",
        "/api/sessions/{session_id}/delegations",
        "/api/models",
    ):
        assert required in paths

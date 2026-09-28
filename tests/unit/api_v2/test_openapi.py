"""``/v2/openapi.json``: generated spec carries the typed contract.

The spec is runtime-derived from the live router and models — these
assertions pin the Session vocabulary, the typed schemas (no loose
``additionalProperties`` blobs on request bodies), and the canonical
error surface shared with /v1.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def spec(client: TestClient) -> dict:
    resp = client.get("/v2/openapi.json")
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestOpenApi:
    def test_all_session_paths(self, spec: dict) -> None:
        paths = spec["paths"]
        expected = {
            ("post", "/v2/sessions"),
            ("get", "/v2/sessions"),
            ("get", "/v2/sessions/{session_id}"),
            ("post", "/v2/sessions/{session_id}/messages"),
            ("get", "/v2/sessions/{session_id}/events"),
            ("post", "/v2/sessions/{session_id}/cancel"),
            ("post", "/v2/sessions/{session_id}/retry"),
            ("get", "/v2/sessions/{session_id}/changes"),
            ("post", "/v2/sessions/{session_id}/deliver"),
        }
        found = {
            (method, path)
            for path, item in paths.items()
            for method in item
            if method in ("get", "post")
        }
        assert expected <= found

    def test_create_request_schema_typed(self, spec: dict) -> None:
        req = spec["paths"]["/v2/sessions"]["post"]["requestBody"]["content"]["application/json"][
            "schema"
        ]
        ref = req["$ref"].split("/")[-1]
        schema = spec["components"]["schemas"][ref]
        prompt = schema["properties"]["prompt"]
        assert prompt["type"] == "string" and prompt["minLength"] == 1
        for field in ("title", "repository", "execution", "delivery", "advanced"):
            assert field in schema["properties"]
        # Strict bodies: no generic additionalProperties blobs.
        assert schema.get("additionalProperties") is False

    def test_session_view_vocabulary(self, spec: dict) -> None:
        schema = spec["components"]["schemas"]["SessionView"]
        assert set(schema["properties"]["status"]["enum"]) == {
            "queued",
            "running",
            "finished",
            "failed",
            "cancelled",
        }
        assert set(schema["properties"]["phase"]["enum"]) == {
            "provisioning",
            "queued",
            "running",
            "delivering",
            "finished",
            "failed",
            "cancelled",
        }
        # Internal id namespaces are not in the response schema.
        for prop in schema["properties"]:
            assert prop not in ("agent_id", "task_id", "run_id", "artifact_refs", "candidates")

    def test_error_body_and_security(self, spec: dict) -> None:
        error = spec["components"]["schemas"]["ErrorBody"]["properties"]["error"]
        for field in ("code", "message", "retryable", "action"):
            assert field in error["properties"]
        assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
        # No 422s — V1Route maps validation failures to the canonical 400.
        for path_item in spec["paths"].values():
            for operation in path_item.values():
                assert "422" not in operation.get("responses", {})

    def test_x_canonical_vocabulary(self, spec: dict) -> None:
        canon = spec["x-canonical"]
        assert canon["session_statuses"] == [
            "queued",
            "running",
            "finished",
            "failed",
            "cancelled",
        ]
        assert "delivering" in canon["session_phases"]
        for t in (
            "session.status",
            "session.meta",
            "turn.started",
            "turn.finished",
            "item.completed",
        ):
            assert t in canon["session_event_types"]

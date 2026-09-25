"""SOR-201 acceptance: indexed agent filtering + keyset pagination on
``GET /v1/artifacts``.

Backward compatibility: the response keeps ``artifacts`` and gains an
additive ``next_cursor``; omitting ``limit``/``cursor`` returns the full
listing exactly as SOR-83 did.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from control.artifacts import InMemoryArtifactStore, build_artifact
from fastapi.testclient import TestClient


def _seed_store(tmp_path: Path) -> InMemoryArtifactStore:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("x\n", encoding="utf-8")
    store = InMemoryArtifactStore()
    for i in range(7):
        base = datetime(2026, 9, 15, tzinfo=UTC) + timedelta(seconds=i)
        build_artifact(
            ws,
            store=store,
            agent_id="agent-a",
            run_id=f"run-{i}",
            artifact_id=f"art-{i}",
            clock=lambda b=base: b,
        )
    for j in range(3):
        base = datetime(2026, 9, 15, tzinfo=UTC) + timedelta(minutes=j)
        build_artifact(
            ws,
            store=store,
            agent_id="agent-b",
            artifact_id=f"art-b{j}",
            clock=lambda b=base: b,
        )
    return store


@pytest.fixture
def seeded(client: TestClient, v1_env, tmp_path: Path) -> InMemoryArtifactStore:
    store = _seed_store(tmp_path)
    v1_env.app.state.artifact_store = store
    return store


def _get(client: TestClient, auth: dict[str, str], **params) -> dict:
    resp = client.get("/v1/artifacts", headers=auth, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestListArtifactsPagination:
    def test_full_listing_unchanged(self, client: TestClient, auth, seeded) -> None:
        body = _get(client, auth)
        assert len(body["artifacts"]) == 10
        assert body["next_cursor"] is None
        assert "download_url" in body["artifacts"][0]

    def test_agent_filter(self, client: TestClient, auth, seeded) -> None:
        body = _get(client, auth, agent_id="agent-a")
        assert [a["artifact_id"] for a in body["artifacts"]] == [f"art-{i}" for i in range(7)]
        body = _get(client, auth, agent_id="agent-b")
        assert [a["artifact_id"] for a in body["artifacts"]] == [f"art-b{j}" for j in range(3)]

    def test_keyset_walk_no_overlap(self, client: TestClient, auth, seeded) -> None:
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            params: dict = {"agent_id": "agent-a", "limit": 3}
            if cursor:
                params["cursor"] = cursor
            body = _get(client, auth, **params)
            seen += [a["artifact_id"] for a in body["artifacts"]]
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert seen == [f"art-{i}" for i in range(7)]

    def test_limit_on_unfiltered(self, client: TestClient, auth, seeded) -> None:
        body = _get(client, auth, limit=4)
        assert len(body["artifacts"]) == 4
        assert body["next_cursor"]
        rest = _get(client, auth, limit=4, cursor=body["next_cursor"])
        assert len(rest["artifacts"]) == 4

    def test_malformed_cursor_400(self, client: TestClient, auth, seeded) -> None:
        resp = client.get("/v1/artifacts?cursor=zzz!!!", headers=auth)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"

    @pytest.mark.parametrize("limit", [0, -1, 501])
    def test_bad_limit_400(self, client: TestClient, auth, seeded, limit) -> None:
        resp = client.get(f"/v1/artifacts?limit={limit}", headers=auth)
        assert resp.status_code == 400

    def test_cursor_reusable_and_stable(self, client: TestClient, auth, seeded) -> None:
        first = _get(client, auth, agent_id="agent-a", limit=2)
        again = _get(client, auth, agent_id="agent-a", limit=2, cursor=first["next_cursor"])
        again2 = _get(client, auth, agent_id="agent-a", limit=2, cursor=first["next_cursor"])
        assert again == again2
        assert [a["artifact_id"] for a in again["artifacts"]] == ["art-2", "art-3"]

    def test_last_page_cursor_is_null(self, client: TestClient, auth, seeded) -> None:
        body = _get(client, auth, agent_id="agent-b", limit=10)
        assert len(body["artifacts"]) == 3
        assert body["next_cursor"] is None

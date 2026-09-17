"""Deterministic tests for the SOR-84/C2 client SDK surface.

All HTTP/SSE is mocked through ``httpx.MockTransport`` — no uvicorn, no
backend, no real sockets. SSE bodies are byte iterators so a mid-stream
``httpx.ReadError`` deterministically simulates a dropped connection.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from examples.sbx_client import (
    SbxApiError,
    SbxClient,
    SbxTransportError,
    SseEvent,
    WorkflowRecovery,
)

BASE = "http://sbx.test"


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> SbxClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    return SbxClient(client=http)


def frame(eid: int, etype: str, data: dict[str, Any]) -> bytes:
    return f"id: {eid}\nevent: {etype}\ndata: {json.dumps(data)}\n\n".encode()


def run_payload(agent_id: str, run_id: str, status: str) -> dict[str, Any]:
    return {
        "id": run_id,
        "agent_id": agent_id,
        "status": status,
        "created_at": "2026-09-15T00:00:00Z",
        "updated_at": "2026-09-15T00:00:01Z",
    }


def agent_payload(agent_id: str, workflow_id: str | None = "wf-1") -> dict[str, Any]:
    agent: dict[str, Any] = {
        "id": agent_id,
        "name": agent_id,
        "provider": "codex",
        "account_id": "auto",
        "model": "m",
        "status": "running",
        "created_at": "2026-09-15T00:00:00Z",
        "updated_at": "2026-09-15T00:00:01Z",
    }
    if workflow_id is not None:
        agent["metadata"] = {"workflow_id": workflow_id, "task_id": f"t-{agent_id}"}
    return agent


# ------------------------------------------------------------------- SSE


def test_stream_run_preserves_sse_id_type_data() -> None:
    body = (
        b": keepalive\n\n"
        + frame(1, "sbx.session_meta", {"type": "sbx.session_meta", "session_id": "s1"})
        + frame(2, "sbx.turn_started", {"type": "sbx.turn_started", "n": 1})
        + frame(3, "item.completed", {"type": "item.completed", "item": {"x": 1}})
    )
    client = make_client(lambda req: httpx.Response(200, content=iter([body])))

    events = list(client.stream_run("ag-1", "run-1"))

    assert [(e.id, e.type) for e in events] == [
        ("1", "sbx.session_meta"),
        ("2", "sbx.turn_started"),
        ("3", "item.completed"),
    ]
    assert events[2].data["item"] == {"x": 1}
    assert isinstance(events[0], SseEvent)


def test_stream_run_sends_last_event_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=iter([frame(8, "x", {"type": "x"})]))

    client = make_client(handler)
    list(client.stream_run("ag-1", "run-1", last_event_id=7))
    assert seen[0].headers["Last-Event-ID"] == "7"


def test_watch_stops_on_terminal_event_without_get() -> None:
    body = (
        b": keepalive\n\n"
        + frame(7, "sbx.turn_started", {"type": "sbx.turn_started", "n": 1})
        + frame(8, "sbx.turn_finished", {"type": "sbx.turn_finished", "n": 1})
        + b": keepalive\n\n"
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=iter([body]))

    client = make_client(handler)
    events = list(client.watch("ag-1", "run-1"))

    assert [e.type for e in events] == ["sbx.turn_started", "sbx.turn_finished"]
    assert [r.url.path for r in requests] == ["/v1/agents/ag-1/runs/run-1/stream"]


def test_watch_reconnects_with_last_event_id() -> None:
    def first() -> Iterator[bytes]:
        yield b": keepalive\n\n"
        yield frame(4, "sbx.turn_started", {"type": "sbx.turn_started", "n": 1})
        raise httpx.ReadError("connection dropped")

    def second() -> Iterator[bytes]:
        yield frame(5, "item.completed", {"type": "item.completed"})
        yield frame(6, "sbx.turn_finished", {"type": "sbx.turn_finished", "n": 1})

    streams = [first, second]
    calls = {"stream": 0}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/stream"):
            i = calls["stream"]
            calls["stream"] += 1
            return httpx.Response(200, content=streams[i]())
        return httpx.Response(200, json=run_payload("ag-1", "run-1", "RUNNING"))

    client = make_client(handler)
    events = [e.type for e in client.watch("ag-1", "run-1", backoff_s=0)]

    assert events == ["sbx.turn_started", "item.completed", "sbx.turn_finished"]
    stream_reqs = [r for r in requests if r.url.path.endswith("/stream")]
    assert len(stream_reqs) == 2
    assert stream_reqs[0].headers.get("Last-Event-ID") is None
    assert stream_reqs[1].headers["Last-Event-ID"] == "4"


def test_watch_falls_back_to_get_terminal() -> None:
    """Reconnect attempts stop as soon as GET reports a persisted terminal."""
    calls = {"stream": 0, "get": 0}

    def dropped() -> Iterator[bytes]:
        yield b": keepalive\n\n"
        raise httpx.ReadError("dropped")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            calls["stream"] += 1
            return httpx.Response(200, content=dropped())
        calls["get"] += 1
        status = "FINISHED" if calls["get"] >= 3 else "RUNNING"
        return httpx.Response(200, json=run_payload("ag-1", "run-1", status))

    client = make_client(handler)
    events = list(client.watch("ag-1", "run-1", max_reconnects=8, backoff_s=0))

    assert events == []
    assert calls["stream"] == 3  # opened, retried, gave up when GET went terminal


def test_watch_reconnect_budget_is_finite() -> None:
    calls = {"stream": 0, "get": 0}

    def dropped() -> Iterator[bytes]:
        yield b": keepalive\n\n"
        raise httpx.ReadError("dropped")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            calls["stream"] += 1
            return httpx.Response(200, content=dropped())
        calls["get"] += 1
        return httpx.Response(200, json=run_payload("ag-1", "run-1", "RUNNING"))

    client = make_client(handler)
    events = list(client.watch("ag-1", "run-1", max_reconnects=3, backoff_s=0))

    assert events == []
    assert calls["stream"] == 4  # initial + max_reconnects, never unbounded


def test_watch_idle_keepalive_polls_terminal_status() -> None:
    """Retention-expired replay keepalives forever — a status poll ends watch."""
    gets = {"n": 0}

    def keepalives() -> Iterator[bytes]:
        while True:
            yield b": keepalive\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, content=keepalives())
        gets["n"] += 1
        return httpx.Response(200, json=run_payload("ag-1", "run-1", "CANCELLED"))

    client = make_client(handler)
    events = list(client.watch("ag-1", "run-1", status_poll_s=0))

    assert events == []
    assert gets["n"] == 1


def test_watch_close_detaches_without_cancelling() -> None:
    requests: list[httpx.Request] = []

    def body() -> Iterator[bytes]:
        yield frame(1, "sbx.turn_started", {"type": "sbx.turn_started", "n": 1})
        yield frame(2, "item.completed", {"type": "item.completed"})
        yield b": keepalive\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=body())

    client = make_client(handler)
    stream = client.watch("ag-1", "run-1")
    assert next(stream).type == "sbx.turn_started"
    stream.close()

    assert [r.url.path for r in requests] == ["/v1/agents/ag-1/runs/run-1/stream"]
    assert not any(r.method == "POST" and r.url.path.endswith("/cancel") for r in requests)


def test_watch_client_4xx_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream"):
            return httpx.Response(
                401, json={"error": {"code": "unauthorized", "message": "bad key"}}
            )
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "bad key"}})

    client = make_client(handler)
    with pytest.raises(SbxApiError, match="unauthorized"):
        list(client.watch("ag-1", "run-1", backoff_s=0))


# ----------------------------------------------------------------- waits


def test_wait_polls_until_terminal() -> None:
    statuses = iter(["RUNNING", "RUNNING", "FINISHED"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=run_payload("ag-1", "run-1", next(statuses)))

    client = make_client(handler)
    run = client.wait("ag-1", "run-1", poll_s=0)
    assert run["status"] == "FINISHED"


def test_wait_timeout_returns_last_observed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=run_payload("ag-1", "run-1", "RUNNING"))

    client = make_client(handler)
    run = client.wait("ag-1", "run-1", timeout_s=0.02, poll_s=0)
    assert run["status"] == "RUNNING"


def test_wait_many_reports_each_run_independently() -> None:
    """Unordered completion: one ERROR and one CANCELLED resolve on their own."""
    seqs: dict[str, list[str]] = {
        "/v1/agents/a/runs/run-1": ["RUNNING", "FINISHED"],
        "/v1/agents/b/runs/run-2": ["ERROR"],
        "/v1/agents/c/runs/run-3": ["RUNNING", "CANCELLED"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        agent_id, run_id = path.split("/")[3], path.split("/")[5]
        status = seqs[path].pop(0) if seqs[path] else "FINISHED"
        return httpx.Response(200, json=run_payload(agent_id, run_id, status))

    client = make_client(handler)
    out = client.wait_many([("a", "run-1"), ("b", "run-2"), ("c", "run-3")], poll_s=0, timeout_s=5)

    assert out[("a", "run-1")]["status"] == "FINISHED"
    assert out[("b", "run-2")]["status"] == "ERROR"
    assert out[("c", "run-3")]["status"] == "CANCELLED"


def test_wait_many_accepts_run_dicts_and_times_out() -> None:
    runs = [
        run_payload("a", "run-1", "RUNNING"),
        run_payload("b", "run-2", "RUNNING"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        agent_id, run_id = path.split("/")[3], path.split("/")[5]
        status = "FINISHED" if run_id == "run-2" else "RUNNING"
        return httpx.Response(200, json=run_payload(agent_id, run_id, status))

    client = make_client(handler)
    out = client.wait_many(runs, timeout_s=0.05, poll_s=0)

    assert out[("b", "run-2")]["status"] == "FINISHED"
    assert out[("a", "run-1")]["status"] == "RUNNING"  # last observed, not inferred


# --------------------------------------------------------------- workflow


def test_create_passes_metadata_workspace_and_idempotency() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            201,
            json={
                "agent": agent_payload("ag-1"),
                "run": run_payload("ag-1", "run-1", "CREATING"),
            },
        )

    client = make_client(handler)
    created = client.create(
        "do work",
        provider="grok",
        metadata={"workflow_id": "wf-1", "task_id": "t-1", "role": "worker"},
        workspace={"repo": "sorenforge/sbx", "base_sha": "abc123"},
        idempotency_key="k-9",
    )

    assert created["run"]["status"] == "CREATING"
    body = json.loads(seen[0].content)
    assert body["metadata"]["workflow_id"] == "wf-1"
    assert body["workspace"]["base_sha"] == "abc123"
    assert seen[0].headers["Idempotency-Key"] == "k-9"


def test_recover_finds_only_workflow_agents() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/agents":
            return httpx.Response(
                200,
                json={
                    "agents": [
                        agent_payload("a1", "wf-1"),
                        agent_payload("a2", "wf-2"),  # other workflow
                        agent_payload("a3", None),  # no metadata: never claimed
                    ],
                    "next_cursor": None,
                },
            )
        assert request.url.path == "/v1/agents/a1/runs"
        return httpx.Response(
            200,
            json={
                "runs": [
                    {**run_payload("a1", "run-1", "FINISHED"), "artifact_refs": ["turns/1.json"]},
                    {
                        **run_payload("a1", "run-2", "RUNNING"),
                        "artifact_refs": ["turns/2.json", "events.jsonl"],
                    },
                ]
            },
        )

    client = make_client(handler)
    rec = client.recover("wf-1")

    assert isinstance(rec, WorkflowRecovery)
    assert [a["id"] for a in rec.agents] == ["a1"]
    assert rec.latest_runs["a1"]["id"] == "run-2"
    assert rec.handles() == [("a1", "run-2")]
    assert rec.artifact_refs() == {"a1": ["turns/2.json", "events.jsonl"]}
    assert "workflow_id=wf-1" in requests[0].url.query.decode()


def test_close_workflow_is_scoped_and_idempotent() -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(request.url.path)
            agent_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=agent_payload(agent_id, "wf-1"))
        if request.url.path == "/v1/agents":
            return httpx.Response(
                200,
                json={
                    "agents": [agent_payload("a1", "wf-1"), agent_payload("a2", "wf-2")],
                    "next_cursor": None,
                },
            )
        return httpx.Response(200, json={"runs": [run_payload("a1", "run-1", "FINISHED")]})

    client = make_client(handler)
    first = client.close_workflow("wf-1")
    second = client.close_workflow("wf-1")

    assert deleted == ["/v1/agents/a1", "/v1/agents/a1"]  # idempotent re-close
    assert first[0]["id"] == "a1" and second[0]["id"] == "a1"


def test_resume_reattaches_to_latest_run() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/agents/a1/runs":
            return httpx.Response(
                200,
                json={
                    "runs": [
                        run_payload("a1", "run-1", "FINISHED"),
                        run_payload("a1", "run-2", "RUNNING"),
                    ]
                },
            )
        return httpx.Response(
            200, content=iter([frame(9, "sbx.turn_finished", {"type": "sbx.turn_finished"})])
        )

    client = make_client(handler)
    events = list(client.resume("a1"))

    assert events[-1].type == "sbx.turn_finished"
    assert requests[-1].url.path == "/v1/agents/a1/runs/run-2/stream"


# ------------------------------------------------------------------ misc


def test_followup_cancel_close_agent_paths() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST" and request.url.path.endswith("/runs"):
            return httpx.Response(201, json=run_payload("a1", "run-2", "RUNNING"))
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            return httpx.Response(200, json=run_payload("a1", "run-2", "CANCELLED"))
        return httpx.Response(200, json=agent_payload("a1"))

    client = make_client(handler)
    follow = client.followup("a1", "second turn", metadata={"task_id": "t-2"})
    cancelled = client.cancel("a1", "run-2")
    closed = client.close_agent("a1")

    assert follow["id"] == "run-2" and cancelled["status"] == "CANCELLED"
    assert closed["status"] == "running"  # payload passthrough
    assert json.loads(seen[0].content)["metadata"] == {"task_id": "t-2"}
    assert [r.method for r in seen] == ["POST", "POST", "DELETE"]


def test_artifacts_seam_list_and_download(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/download"):
            return httpx.Response(200, content=b"artifact-bytes")
        return httpx.Response(
            200,
            json={
                "artifacts": [
                    {
                        "id": "art-1",
                        "kind": "bundle",
                        "sha256": "0" * 64,
                        "producer": {"agent_id": "a1", "run_id": "run-2"},
                    },
                    {
                        "id": "art-2",
                        "producer": {"agent_id": "a1", "run_id": "run-1"},
                    },
                ]
            },
        )

    client = make_client(handler)
    listed = client.artifacts.list("a1", run_id="run-2")
    dest = tmp_path / "art.bin"
    data = client.artifacts.download("a1", "art-1", dest=dest)

    assert [a["id"] for a in listed] == ["art-1"]  # run_id filters on producer
    assert data == b"artifact-bytes" == dest.read_bytes()
    assert requests[0].url.path == "/v1/artifacts"
    assert requests[0].url.params["agent_id"] == "a1"
    assert requests[1].url.path == "/v1/artifacts/art-1/download"


def test_error_unwraps_canonical_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"error": {"code": "not_found", "message": "agent not found"}}
        )

    client = make_client(handler)
    with pytest.raises(SbxApiError, match="not_found") as excinfo:
        client.get_agent("nope")
    assert excinfo.value.status == 404
    assert excinfo.value.code == "not_found"


# ---------------------------------------------------- SOR-118: timeouts


def test_normal_verbs_carry_finite_timeout() -> None:
    """GET/POST/DELETE all ride a finite connect/read/write/pool budget."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200, json=agent_payload("a1"))
        if request.method == "POST":
            return httpx.Response(
                200, json={"workspace": {"agent_id": "a1", "reviewed_head_sha": "h"}}
            )
        return httpx.Response(200, json=agent_payload("a1"))

    client = make_client(handler)
    client.get_agent("a1")
    client.review_workspace("a1")
    client.close_agent("a1")

    assert [r.method for r in seen] == ["GET", "POST", "DELETE"]
    for request in seen:
        budget = request.extensions["timeout"]
        for phase in ("connect", "read", "write", "pool"):
            assert budget[phase] is not None and budget[phase] > 0


def test_env_var_sets_uniform_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBX_HTTP_TIMEOUT_S", "2.5")
    seen: list[httpx.Request] = []

    client = make_client(
        lambda req: (seen.append(req), httpx.Response(200, json=agent_payload("a1")))[1]
    )
    client.get_agent("a1")

    budget = seen[0].extensions["timeout"]
    assert budget == {"connect": 2.5, "read": 2.5, "write": 2.5, "pool": 2.5}


def test_review_timeout_names_durable_get() -> None:
    """A review that may have persisted must point at GET + reviewed_head_sha."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client = make_client(handler)
    with pytest.raises(SbxTransportError) as excinfo:
        client.review_workspace("a1", head_sha="h" * 8)

    err = excinfo.value
    assert err.check == "GET /v1/agents/a1/workspace (inspect reviewed_head_sha)"
    assert err.idempotent is True
    assert "reviewed_head_sha" in str(err)
    assert "durable state" in str(err)
    assert isinstance(err.original, httpx.ReadTimeout)


def test_artifact_create_timeout_directs_to_list_not_repost() -> None:
    """Artifact create is not idempotent — the hint is 'list first'."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = make_client(handler)
    with pytest.raises(SbxTransportError) as excinfo:
        client.create_artifact("a1", run_id="run-1")

    err = excinfo.value
    assert err.check == "GET /v1/artifacts?agent_id=a1"
    assert err.idempotent is False
    assert "may already be persisted" in str(err)


def test_response_loss_after_persisted_review_recovers_via_get() -> None:
    """The mutation landed but the response was lost: GET reveals the pin."""
    state = {"reviewed_head_sha": None}
    head = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/workspace/review"):
            state["reviewed_head_sha"] = json.loads(request.content)["head_sha"]
            raise httpx.ReadTimeout("response lost", request=request)
        assert request.url.path == "/v1/agents/a1/workspace"
        return httpx.Response(
            200,
            json={
                "workspace": {
                    "agent_id": "a1",
                    "repo": "o/r",
                    "base_ref": "main",
                    "base_sha": "b" * 40,
                    "reviewed_head_sha": state["reviewed_head_sha"],
                }
            },
        )

    client = make_client(handler)
    with pytest.raises(SbxTransportError):
        client.review_workspace("a1", head_sha=head)
    assert client.get_workspace("a1")["reviewed_head_sha"] == head


def test_artifact_download_timeout_is_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow download", request=request)

    client = make_client(handler)
    with pytest.raises(SbxTransportError):
        client.artifacts.download("a1", "art-1")


def test_stream_uses_long_read_gap_not_unbounded_connect() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, content=iter([frame(1, "sbx.turn_finished", {"type": "sbx.turn_finished"})])
        )

    client = make_client(handler)
    list(client.stream_run("a1", "run-1"))

    budget = seen[0].extensions["timeout"]
    assert budget["read"] == 90.0  # the stream's own long-lived read gap
    for phase in ("connect", "write", "pool"):
        assert budget[phase] is not None and budget[phase] > 0


def test_wait_survives_a_transient_transport_error() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=run_payload("ag-1", "run-1", "FINISHED"))

    client = make_client(handler)
    run = client.wait("ag-1", "run-1", poll_s=0)
    assert run["status"] == "FINISHED"
    assert calls["n"] == 2


def test_wait_raises_last_transport_error_when_no_get_succeeded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = make_client(handler)
    with pytest.raises(SbxTransportError):
        client.wait("ag-1", "run-1", timeout_s=0.02, poll_s=0)

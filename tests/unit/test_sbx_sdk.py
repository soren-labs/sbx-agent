"""SOR-226: the productized ``sbx.sdk`` — typed Task/Run/Revision/Delivery/
Review namespaces, canonical error decode, automatic idempotency."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from examples.sbx_client import SbxApiError, SbxClient, SbxTransportError

from sbx.sdk import (
    RUN_TERMINAL,
    Agent,
    Review,
    Revision,
    Run,
    Task,
    TaskCreated,
    TaskDetail,
)

BASE = "http://sbx.test"


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> SbxClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    return SbxClient(client=http)


def run_payload(agent_id: str, run_id: str, status: str) -> dict[str, Any]:
    return {
        "id": run_id,
        "agent_id": agent_id,
        "status": status,
        "created_at": "2026-09-15T00:00:00Z",
        "updated_at": "2026-09-15T00:00:01Z",
    }


def task_payload(task_id: str, agent_id: str, status: str = "running") -> dict[str, Any]:
    return {
        "id": task_id,
        "status": status,
        "request": {"prompt": {"text": "hi"}},
        "agent_id": agent_id,
        "run_id": "run-1",
        "created_at": "2026-09-15T00:00:00Z",
        "updated_at": "2026-09-15T00:00:01Z",
    }


def detail_payload(task_id: str, agent_id: str) -> dict[str, Any]:
    return {
        "task": task_payload(task_id, agent_id),
        "agent": {"id": agent_id, "status": "running"},
        "run": run_payload(agent_id, "run-1", "RUNNING"),
        "runs": [run_payload(agent_id, "run-1", "RUNNING")],
    }


def revision_payload(rev_id: str = "rev-1", n: int = 1) -> dict[str, Any]:
    return {
        "id": rev_id,
        "n": n,
        "agent_id": "ag-1",
        "task_id": "task-1",
        "run_id": "run-1",
        "artifact_id": "art-1",
        "repo": "https://github.com/o/r",
        "base_sha": "b" * 40,
        "head_sha": "h" * 40,
        "status": "ready",
        "error": None,
        "delivery": None,
        "created_at": "2026-09-15T00:00:00Z",
        "updated_at": "2026-09-15T00:00:01Z",
    }


def test_typed_task_create_returns_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "task": task_payload("t-1", "ag-1"),
                "agent": {"id": "ag-1"},
                "run": run_payload("ag-1", "run-1", "QUEUED"),
            },
        )

    client = make_client(handler)
    created = client.tasks.create("do work", source={"repo": "o/r"})

    assert isinstance(created, TaskCreated)
    assert isinstance(created.task, Task) and created.task.id == "t-1"
    assert isinstance(created.agent, Agent) and created.agent.id == "ag-1"
    assert isinstance(created.run, Run) and created.run.status == "QUEUED"
    assert created.run.terminal is False


def test_typed_get_list_and_envelope_decode() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/tasks":
            return httpx.Response(200, json={"tasks": [task_payload("t-1", "ag-1")]})
        return httpx.Response(200, json=detail_payload("t-1", "ag-1"))

    client = make_client(handler)
    tasks = client.tasks.list()
    detail = client.tasks.get("t-1")

    assert [t.id for t in tasks] == ["t-1"]
    assert isinstance(detail, TaskDetail)
    assert detail.task.id == "t-1" and detail.run.status == "RUNNING"
    assert [r.id for r in detail.runs] == ["run-1"]


def test_task_followup_cancel_retry_delivery_merge_flow() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/revisions/latest"):
            return httpx.Response(200, json={"revision": revision_payload()})
        if request.url.path.endswith("/reviews"):
            if request.method == "POST":
                return httpx.Response(
                    201,
                    json={
                        "review": {
                            "id": "rvw-1",
                            "revision_id": "rev-1",
                            "agent_id": "ag-1",
                            "reviewer": {"identity": "key-1"},
                            "verdict": "approve",
                            "independent": True,
                            "stale": False,
                        }
                    },
                )
            return httpx.Response(200, json={"reviews": []})
        if request.url.path.endswith(("/deliver", "/merge")):
            rev = revision_payload()
            rev["delivery"] = {"status": "delivered", "pull_request": {"number": 7}}
            return httpx.Response(200, json={"revision": rev})
        return httpx.Response(200, json=detail_payload("t-1", "ag-1"))

    client = make_client(handler)
    assert isinstance(client.tasks.cancel("t-1"), TaskDetail)
    assert isinstance(client.tasks.retry("t-1"), TaskDetail)
    assert isinstance(client.tasks.delivery("t-1"), TaskDetail)

    rev = client.tasks.deliver("t-1")
    assert isinstance(rev, Revision) and rev.delivery["status"] == "delivered"

    rev2 = client.tasks.revision("t-1")
    assert rev2.id == "rev-1" and rev2.n == 1

    review = client.tasks.review("t-1", verdict="approve")
    assert isinstance(review, Review) and review.verdict == "approve"
    assert review.independent is True and review.stale is False

    merged = client.tasks.merge("t-1")
    assert merged.delivery["pull_request"]["number"] == 7

    assert "POST /v1/tasks/t-1/cancel" in seen
    assert "GET /v1/tasks/t-1/revisions/latest" in seen


def test_auto_idempotency_key_on_keyed_routes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/tasks":
            return httpx.Response(
                201,
                json={
                    "task": task_payload("t-1", "ag-1"),
                    "agent": {"id": "ag-1"},
                    "run": run_payload("ag-1", "run-1", "QUEUED"),
                },
            )
        if request.url.path.endswith("/reviews"):
            return httpx.Response(201, json={"review": {"id": "r1"}})
        return httpx.Response(200, json=detail_payload("t-1", "ag-1"))

    client = make_client(handler)
    client.tasks.create("work")
    client.tasks.review("t-1", verdict="approve")
    client.tasks.cancel("t-1")

    task_post = next(r for r in seen if r.url.path == "/v1/tasks")
    review_post = next(r for r in seen if r.url.path.endswith("/reviews"))
    cancel_post = next(r for r in seen if r.url.path.endswith("/cancel"))

    # /v1/tasks honors Idempotency-Key server-side → auto-keyed.
    assert task_post.headers["Idempotency-Key"].startswith("sdk-")
    # Reviews/cancel are not keyed routes → no header is forged.
    assert "Idempotency-Key" not in review_post.headers
    assert "Idempotency-Key" not in cancel_post.headers


def test_keyed_transport_error_retries_once() -> None:
    attempts: list[httpx.Request] = []
    fails = {"left": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if fails["left"]:
            fails["left"] -= 1
            raise httpx.ConnectError("reset", request=request)
        return httpx.Response(
            201,
            json={
                "task": task_payload("t-1", "ag-1"),
                "agent": {"id": "ag-1"},
                "run": run_payload("ag-1", "run-1", "QUEUED"),
            },
        )

    client = make_client(handler)
    created = client.tasks.create("work")
    assert created.task.id == "t-1"
    assert len(attempts) == 2  # pinned Idempotency-Key → safe replay
    assert attempts[0].headers["Idempotency-Key"] == attempts[1].headers["Idempotency-Key"]


def test_unkeyed_transport_error_does_not_retry() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("refused", request=request)

    client = make_client(handler)
    with pytest.raises(SbxTransportError) as excinfo:
        client.tasks.review("t-1", verdict="approve")
    assert len(attempts) == 1
    assert excinfo.value.idempotent is False
    assert excinfo.value.check == "GET /v1/tasks/t-1/reviews"


def test_error_decode_canonical_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "error": {
                    "code": "provider_exhausted",
                    "message": "no free account",
                    "retryable": True,
                    "action": "retry",
                    "retry_after": 30,
                    "details": {"eligible": 0},
                }
            },
        )

    client = make_client(handler)
    with pytest.raises(SbxApiError) as excinfo:
        client.tasks.list()
    err = excinfo.value
    assert (err.status, err.code) == (429, "provider_exhausted")
    assert err.retryable is True and err.action == "retry"
    assert err.retry_after == 30 and err.details == {"eligible": 0}


def test_sdk_shim_reexports_and_compat_api() -> None:
    """examples.sbx_client stays a working alias for the productized SDK."""
    import examples.sbx_client as compat

    import sbx.sdk as sdk

    assert compat.SbxClient is sdk.SbxClient
    assert compat.SbxApiError is sdk.SbxApiError
    assert compat.SseEvent is sdk.SseEvent
    assert compat.WorkflowRecovery is sdk.WorkflowRecovery
    assert compat.RUN_TERMINAL == RUN_TERMINAL


def test_runs_namespace_stream_and_wait() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=run_payload("ag-1", "run-1", "FINISHED"))

    client = make_client(handler)
    run = client.runs.wait("ag-1", "run-1", poll_s=0.01)
    assert run.status == "FINISHED" and run.terminal is True


def test_agents_revisions_namespace() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"revisions": [revision_payload()]})

    client = make_client(handler)
    revs = client.agents.revisions("ag-1")
    assert isinstance(revs[0], Revision) and revs[0].agent_id == "ag-1"


def test_openapi_fetch() -> None:
    spec = {"openapi": "3.1.0", "paths": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=spec)

    client = make_client(handler)
    assert client.openapi() == spec

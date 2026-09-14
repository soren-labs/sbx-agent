"""Walkthrough of examples/sbx_client.py against a live uvicorn server."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.sbx_client import SbxClient  # noqa: E402


def _wait(client: SbxClient, agent_id: str, run_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get_run(agent_id, run_id)
        if run["status"] in ("FINISHED", "ERROR", "CANCELLED", "EXPIRED"):
            return run
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not reach a terminal state")


@pytest.fixture()
def sbx(live_base, v1_env) -> SbxClient:
    client = SbxClient(base_url=live_base, api_key=v1_env.agents_token)
    yield client
    client.close()


def test_sbx_client_flow(sbx: SbxClient) -> None:
    assert sbx.me()["key_id"]
    assert sbx.models()

    created = sbx.create_agent("Say hello")
    agent, run = created["agent"], created["run"]
    assert agent["status"] in ("running", "idle")
    assert run["status"] in ("RUNNING", "FINISHED")

    types = set()
    for event in sbx.stream_run(agent["id"], run["id"]):
        types.add(event.get("type"))
        if event.get("type") == "sbx.turn_finished":
            break
    assert "sbx.turn_finished" in types

    run = _wait(sbx, agent["id"], run["id"])
    assert run["status"] == "FINISHED"
    assert run["result"]["text"]

    follow = sbx.create_run(agent["id"], "one more time")
    follow = _wait(sbx, agent["id"], follow["id"])
    assert follow["status"] == "FINISHED"
    assert len(sbx.list_runs(agent["id"])) == 2

    cancelled = sbx.cancel_run(agent["id"], follow["id"])
    assert cancelled["status"] == "FINISHED"  # terminal run: returns latest state

    usage = sbx.usage(agent["id"])
    assert set(usage) == {"usage", "cost_estimate_usd", "sandbox_seconds"}

    closed = sbx.delete_agent(agent["id"])
    assert closed["status"] == "closed"


def test_sbx_client_error_unwraps_canonical_body(sbx: SbxClient) -> None:
    with pytest.raises(RuntimeError, match="not_found"):
        sbx.get_agent("nope")

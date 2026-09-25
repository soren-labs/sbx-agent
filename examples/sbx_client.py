#!/usr/bin/env python3
"""Backward-compatible entry point — the real client is ``sbx.sdk.SbxClient``.

The SOR-84/C2 client prototype was productized as the formal SDK under the
``sbx`` package (SOR-226): same verbs, plus typed Task/Run/Revision/
Delivery/Review operations under ``client.tasks`` / ``client.agents`` /
``client.runs`` / ``client.artifacts``, automatic ``Idempotency-Key``
handling and the canonical ``retryable``/``action``/``details`` error
fields. This module re-exports that implementation so existing imports keep
working.

Usage:
    SBX_API_KEY=sbx_... SBX_BASE_URL=https://sbx.sorenforge.com \
        python examples/sbx_client.py "Write hello.txt containing hi"
"""

from __future__ import annotations

import json
import sys

from sbx.sdk import (  # noqa: F401 — compat re-exports
    RUN_DONE,
    RUN_END_EVENTS,
    RUN_TERMINAL,
    TASK_TERMINAL,
    Agent,
    Delivery,
    Review,
    Revision,
    Run,
    RunError,
    SbxApiError,
    SbxClient,
    SbxTransportError,
    SseEvent,
    Task,
    TaskCreated,
    TaskDetail,
    WorkflowRecovery,
)
from sbx.sdk.client import (  # noqa: F401 — compat re-exports
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    TIMEOUT_ENV,
    _sse_frames,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "TIMEOUT_ENV",
    "RUN_DONE",
    "RUN_END_EVENTS",
    "RUN_TERMINAL",
    "TASK_TERMINAL",
    "Agent",
    "Delivery",
    "Revision",
    "Review",
    "Run",
    "RunError",
    "SbxApiError",
    "SbxClient",
    "SbxTransportError",
    "SseEvent",
    "Task",
    "TaskCreated",
    "TaskDetail",
    "WorkflowRecovery",
    "main",
]


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Create hello.txt containing 'hi'."
    client = SbxClient()
    try:
        created = client.create_agent(prompt, provider="codex")
        agent, run = created["agent"], created["run"]
        print(f"agent {agent['id']} ({agent['status']}); first run {run['id']} ({run['status']})")
        for event in client.watch(agent["id"], run["id"]):
            if event.type.startswith("sbx."):
                print(f"  event {event.id} {event.type}")
        run = client.wait(agent["id"], run["id"])
        result_text = (run.get("result") or {}).get("text", "")[:80]
        print(f"run {run['id']} -> {run['status']}: {result_text}")
        follow = client.followup(agent["id"], "Now append a second line.")
        print(f"follow-up {follow['id']} ({follow['status']}); cancelling")
        cancelled = client.cancel(agent["id"], follow["id"])
        print(f"cancelled -> {cancelled['status']}")
        print("usage:", json.dumps(client.usage(agent["id"])))
        closed = client.close_agent(agent["id"])
        print(f"agent {closed['id']} -> {closed['status']}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())

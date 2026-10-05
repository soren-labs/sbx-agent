"""Opt-in live smoke: Modal executor + sbx-runtime + official OpenCode CLI.

Reads SBX_TEST_MODAL_TOKEN_ID/SECRET and OPENCODE_ZEN_API_KEY from the environment,
never prints them, and always terminates the sandbox. Not collected by pytest.
"""

from __future__ import annotations

import os
import secrets
import sys
import time

from control.executors.modal import ModalExecutor
from control.runtime_client.client import HttpRuntimeConnector


def main() -> int:
    compute = {
        "token_id": os.environ["SBX_TEST_MODAL_TOKEN_ID"],
        "token_secret": os.environ["SBX_TEST_MODAL_TOKEN_SECRET"],
    }
    executor = ModalExecutor()
    connector = HttpRuntimeConnector(secrets.token_bytes(32))
    lease = {"id": f"lease_smoke{int(time.time())}", "generation": 1}
    op = f"op_smoke{int(time.time())}"
    spec = {
        "workspace_id": "wsp_smoke",
        "session_id": "sess_smoke",
        "lease_id": lease["id"],
        "generation": 1,
        "resource_class": "small",
        "enrollment_key": connector.enrollment_key(lease),
        "compute": compute,
    }
    t0 = time.time()
    handle = executor.allocate(spec, op)
    print("allocated", round(time.time() - t0, 1), "s")
    try:
        assert executor.lookup(op, compute)["sandbox_id"] == handle["sandbox_id"], "tag lookup"
        endpoint = executor.connect_runtime(handle, compute)
        channel = connector.channel({**lease, "handle": {**handle, "endpoint": endpoint}})
        hello = channel.hello()
        print(
            "hello",
            hello["protocol"],
            [(h["provider_id"], h["cli_version"]) for h in hello["harnesses"]],
        )
        print(
            "restore",
            channel.op("worktree.restore", "op_r", "sess_smoke", {"generation": 0})["status"],
        )
        payload = {
            "provider_id": "opencode",
            "turn_id": "turn_s",
            "execution_id": "exec_s",
            "prompt": "Create hello.txt containing sbx and reply DONE.",
            "model": "opencode/big-pickle",
            "native_binding": None,
            "deadline_seconds": 300,
        }
        print(
            "start",
            channel.op(
                "turn.start",
                "exec_s",
                "sess_smoke",
                payload,
                secrets={"opencode_zen": {"api_key": os.environ["OPENCODE_ZEN_API_KEY"]}},
            )["status"],
        )
        for _ in range(300):
            status = channel.query("operation.status", operation_id="exec_s")
            if status["status"] in ("succeeded", "failed", "lost"):
                break
            time.sleep(1)
        print("turn", status["status"], status.get("result", {}).get("verdict"))
        types = [e["type"] for e in channel.events(0)["items"]]
        print("events", len(types), types[:3], types[-2:])
        print("file", channel.query("files.read", path="hello.txt").get("content", "").strip()[:20])
    finally:
        print("terminated", executor.terminate(handle, op + ":t", compute))
    return 0


if __name__ == "__main__":
    sys.exit(main())

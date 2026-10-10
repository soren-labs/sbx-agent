"""Opt-in live smoke: Modal VM executor + sbx-runtime + official OpenCode CLI (BYOK).

Reads SBX_TEST_MODAL_TOKEN_ID/SECRET and SBX_TEST_INFERENCE_API_KEY from the environment,
never prints them, and always terminates the sandbox. Not collected by pytest.

Proves the unified runtime on a real cloud VM: image prewarm vs. cached resolve, per-stage
provisioning timings, a full Linux kernel (not gVisor), and a real custom-API Turn.
``SBX_SMOKE_OUTPUT`` optionally names a file for the sanitized JSON report.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time

from control.executors.modal import ModalExecutor
from control.runtime_client.client import HttpRuntimeConnector

from tests.e2e_modal.inference import inference_credential


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
    report: dict = {"sdk": executor.sdk.__version__, "recipe_digest": executor.recipe_digest()}
    report["prewarm"] = executor.prewarm(compute, "smoke")
    print("prewarm", report["prewarm"])
    t0 = time.time()
    handle = executor.allocate(spec, op)
    print("allocated", round(time.time() - t0, 1), "s", handle["runtime"], handle["timings"])
    report.update(sandbox_id=handle["sandbox_id"], runtime=handle["runtime"])
    report["timings"] = dict(handle["timings"])
    try:
        assert handle["runtime"] == "vm"
        assert executor.lookup(op, compute)["sandbox_id"] == handle["sandbox_id"], "tag lookup"
        t1 = time.time()
        endpoint = executor.connect_runtime(handle, compute)
        report["timings"]["runtime_connect_ms"] = int((time.time() - t1) * 1000)
        report["timings"]["allocate_to_reachable_ms"] = int((time.time() - t0) * 1000)
        probe = executor._sandbox(handle, compute).exec(
            "sh",
            "-c",
            "uname -sr; cat /proc/sys/kernel/random/boot_id; cat /proc/1/comm;"
            " dmesg 2>/dev/null | grep -ci gvisor || true",
        )
        kernel, boot_id, pid1, gvisor_mentions = probe.stdout.read().split("\n")[:4]
        probe.wait()
        report["vm"] = {
            "kernel": kernel,
            "boot_id": boot_id,
            "pid1": pid1,
            "gvisor_dmesg_lines": int(gvisor_mentions or 0),
        }
        print("vm", report["vm"])
        assert kernel.startswith("Linux") and report["vm"]["gvisor_dmesg_lines"] == 0
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
        credential = inference_credential()
        payload = {
            "provider_id": "opencode",
            "turn_id": "turn_s",
            "execution_id": "exec_s",
            "prompt": "Create hello.txt containing sbx and reply DONE.",
            "model": credential["model"],
            "native_binding": None,
            "deadline_seconds": 300,
            "inference": {
                "protocol": "openai_chat",
                "base_url": credential["endpoints"]["openai_chat"],
                "model": credential["model"],
            },
        }
        t_turn = time.time()
        print(
            "start",
            channel.op(
                "turn.start",
                "exec_s",
                "sess_smoke",
                payload,
                secrets={"inference": {"api_key": credential["api_key"]}},
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
        content = channel.query("files.read", path="hello.txt").get("content", "").strip()[:20]
        print("file", content)
        report["turn"] = {
            "harness": "opencode",
            "model": credential["model"],
            "status": status["status"],
            "events": len(types),
            "file_written": content == "sbx",
            "start_to_done_ms": int((time.time() - t_turn) * 1000),
        }
    finally:
        report["terminated"] = executor.terminate(handle, op + ":t", compute)
        print("terminated", report["terminated"])
        if os.environ.get("SBX_SMOKE_OUTPUT"):
            with open(os.environ["SBX_SMOKE_OUTPUT"], "w") as out:
                out.write(json.dumps(report, indent=2) + "\n")
    ok = report.get("turn", {}).get("status") == "succeeded" and report["terminated"]
    print("SMOKE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

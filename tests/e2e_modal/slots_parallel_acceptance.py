"""Opt-in live acceptance: real Sessions on Machine Slots, in parallel, on fresh Modal VMs.

Reads SBX_TEST_MODAL_TOKEN_ID/SECRET and ``SBX_TEST_SLOT_VOLUMES`` (``label=volume,...``,
Volumes that already hold a user-approved official Codex login) from the environment and
never prints credentials. Not collected by pytest. Nothing here reads a profile Volume.

Every Slot is adopted and verified through the API, its model catalog is the one the
authenticated CLI reports, and each round runs one real Codex Turn per Slot at the same
time with a catalogued model and one of that model's own reasoning efforts. Each Turn
runs a shell command in its VM, so the transcript carries that VM's boot ID.

``SBX_SLOT_ROUNDS`` (default ``A1+A2,A1+B1,A1+A2+B1``) names the rounds.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from control.composition import build_worker  # noqa: E402
from control.executors.modal import ModalExecutor  # noqa: E402
from control.integrations.subscriptions.codex import ADAPTER  # noqa: E402
from control.persistence.database import Database  # noqa: E402

from tests.e2e_modal.slots_acceptance import (  # noqa: E402
    COMPUTE,
    running_setup_vms,
    start_postgres,
    volume_exists,
)
from tests.support.api import ApiStack, User  # noqa: E402

REPORT: dict[str, Any] = {"rounds": [], "gates": {}, "slots": {}}
BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
PROMPT = (
    "Run `cat /proc/sys/kernel/random/boot_id` in the shell exactly once. "
    "Reply with only that output followed by a space and the word {marker}."
)


def gate(name: str, ok: Any) -> None:
    REPORT["gates"][name] = bool(ok)
    print(("[ok] " if ok else "[FAIL] ") + name, flush=True)


def pick(catalog: dict[str, Any], index: int) -> tuple[str, str]:
    """A catalogued model and one of its own efforts, varied per Slot. Nothing is hardcoded."""
    models = [m for m in catalog["models"] if len(m["reasoning"]["efforts"]) >= 2]
    model = models[index % len(models)]
    efforts = [e["id"] for e in model["reasoning"]["efforts"]]
    # The lighter half keeps the check quick while still differing between Slots.
    light = efforts[: max(2, len(efforts) // 2)]
    return model["id"], light[index % len(light)]


def effort_probe(executor: ModalExecutor, volume: str, catalog: dict[str, Any]) -> dict[str, Any]:
    """Show the effort is the CLI's native setting and changes what the provider does.

    The same prompt runs at the model's lowest and highest listed effort (reasoning tokens
    and duration are reported by the provider), and once with a value that is no effort at
    all, which the CLI must refuse. Raw output never leaves the VM.
    """
    model = next(m for m in catalog["models"] if len(m["reasoning"]["efforts"]) >= 2)
    efforts = [e["id"] for e in model["reasoning"]["efforts"]]
    inner = r"""
import json, subprocess, sys, time
out = {}
prompt = ("Without using tools: how many integers n with 1 <= n <= 5000 are divisible by "
          "neither 2, 3, 5 nor 7, and what is the largest such n? Reply with the two numbers.")
for label, effort in (("lowest", sys.argv[2]), ("highest", sys.argv[3]), ("not_an_effort", "sbx-bogus")):
    started = time.time()
    p = subprocess.run(
        ["codex", "-c", 'cli_auth_credentials_store="file"', "--disable", "shell_tool", "exec",
         "--ephemeral", "--json", "--skip-git-repo-check", "--sandbox", "read-only",
         "-m", sys.argv[1], "-c", "model_reasoning_effort=" + json.dumps(effort),
         "-C", "/tmp", prompt],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=280)
    usage = {}
    for line in p.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "turn.completed":
            usage = {k: v for k, v in (event.get("usage") or {}).items() if isinstance(v, int)}
    out[label] = {"effort": effort, "returncode": p.returncode,
                  "turn_completed": '"turn.completed"' in p.stdout,
                  "seconds": round(time.time() - started, 1), "usage": usage,
                  "cli_named_the_setting": "model_reasoning_effort" in (p.stdout + p.stderr)}
print(json.dumps(out))
"""
    supported, unsupported = efforts[0], efforts[-1]
    model = model["id"]
    client = executor._client(COMPUTE)
    app = executor._app(client)
    image = executor.image(client, app, "acceptance")
    mounted = executor.sdk.Volume.from_name(
        volume, create_if_missing=False, version=2, client=client
    )
    operation = f"op_effortprobe{int(time.time())}"
    sandbox = executor._create(
        client,
        app,
        image,
        operation,
        {"sbx_alloc": operation, "sbx_setup": "effort-probe"},
        ("sleep", "950"),
        env=dict(ADAPTER.profile_env),
        volumes={"/profile": mounted},
        timeout=950,
        cpu=1.0,
        memory=1024,
    )
    try:
        process = sandbox.exec("python", "-c", inner, model, supported, unsupported, timeout=900)
        output = process.stdout.read()
        process.wait()
        return {"model": model, **json.loads(output)}
    finally:
        sandbox.terminate()


def run(stack: ApiStack, executor: ModalExecutor) -> None:
    user = User(stack)
    REPORT["workspace_id"] = user.workspace_id
    modal = user.connect("modal", COMPUTE)
    slots_path = f"/api/workspaces/{user.workspace_id}/machine-slots"
    wait = lambda cond, timeout=300: _wait(cond, timeout)  # noqa: E731
    wait(lambda: user.get(f"/api/connections/{modal['id']}").json()["health"] == "ready")

    def view(slot_id: str) -> dict[str, Any]:
        return user.get(f"/api/machine-slots/{slot_id}").json()

    slots: dict[str, dict[str, Any]] = {}
    pairs = [p.split("=", 1) for p in os.environ["SBX_TEST_SLOT_VOLUMES"].split(",") if p]
    for label, volume in pairs:
        created = user.post(
            slots_path, {"provider": "codex", "label": label, "volume_name": volume}
        )
        assert created.status_code == 201, created.text
        slots[label] = created.json()
    for index, label in enumerate(slots):
        wait(lambda s=slots[label]: view(s["id"])["status"] != "login_pending")
        slot = slots[label] = view(slots[label]["id"])
        catalog = slot["capabilities"].get("catalog") or {}
        model, effort = pick(catalog, index) if catalog.get("models") else (None, None)
        slot["_choice"] = {"model": model, "effort": effort}
        REPORT["slots"][label] = {
            "status": slot["status"],
            "catalog_status": catalog.get("status"),
            "catalog_source": catalog.get("source"),
            "cli_version": catalog.get("cli_version"),
            "models": [
                {"id": m["id"], "efforts": [e["id"] for e in m["reasoning"]["efforts"]]}
                for m in catalog.get("models") or []
            ],
            "default_model": catalog.get("default_model"),
            "chosen": slot["_choice"],
        }
        gate(
            f"{label}_ready_with_authenticated_catalog",
            slot["status"] == "ready"
            and catalog.get("status") == "ready"
            and len(catalog.get("models") or []) > 0,
        )

    first = next(iter(slots.values()))
    catalog = first["capabilities"]["catalog"]
    invented = user.post(
        f"/api/workspaces/{user.workspace_id}/sessions",
        {
            "harness": {"provider_id": "codex", "model": "sbx-not-a-real-model"},
            "executor": {"backend": "modal"},
            "inference": {"mode": "subscription", "machine_slot_id": first["id"]},
        },
    )
    gate("server_rejects_a_model_outside_the_catalog", invented.status_code == 422)
    partial = next(
        (
            m
            for m in catalog["models"]
            if {e["id"] for x in catalog["models"] for e in x["reasoning"]["efforts"]}
            - {e["id"] for e in m["reasoning"]["efforts"]}
        ),
        None,
    )
    if partial:
        missing = sorted(
            {e["id"] for x in catalog["models"] for e in x["reasoning"]["efforts"]}
            - {e["id"] for e in partial["reasoning"]["efforts"]}
        )[0]
        refused = user.post(
            f"/api/workspaces/{user.workspace_id}/sessions",
            {
                "harness": {"provider_id": "codex", "model": partial["id"], "effort": missing},
                "executor": {"backend": "modal"},
                "inference": {"mode": "subscription", "machine_slot_id": first["id"]},
            },
        )
        gate("server_rejects_an_effort_the_model_does_not_list", refused.status_code == 422)

    REPORT["effort_probe"] = probe = effort_probe(executor, first["volume"]["name"], catalog)
    low, high, bogus = probe["lowest"], probe["highest"], probe["not_an_effort"]
    gate(
        "lowest_and_highest_listed_efforts_complete_real_turns",
        low["turn_completed"] and high["turn_completed"],
    )
    gate(
        "cli_refuses_a_value_that_is_not_an_effort",
        not bogus["turn_completed"] and bogus["returncode"] != 0,
    )
    reasoning = [x["usage"].get("reasoning_output_tokens") for x in (low, high)]
    if None not in reasoning:
        gate("highest_effort_spends_more_reasoning_tokens_than_lowest", reasoning[1] > reasoning[0])

    boots: list[str] = []
    rounds = os.environ.get("SBX_SLOT_ROUNDS", "A1+A2,A1+B1,A1+A2+B1").split(",")
    for number, names in enumerate(rounds, 1):
        labels = names.split("+")
        record: dict[str, Any] = {"slots": labels, "turns": {}}
        REPORT["rounds"].append(record)
        sessions: dict[str, dict[str, Any]] = {}
        for label in labels:
            slot = slots[label]
            marker = f"SBX{number}{label}"
            created = user.post(
                f"/api/workspaces/{user.workspace_id}/sessions",
                {
                    "title": f"round {number} {label}",
                    "harness": {"provider_id": "codex", **slot["_choice"]},
                    "executor": {"backend": "modal", "resource_class": "small"},
                    "inference": {"mode": "subscription", "machine_slot_id": slot["id"]},
                    "message": {"content": PROMPT.format(marker=marker)},
                },
            )
            assert created.status_code == 202, created.text
            sessions[label] = {**created.json(), "marker": marker}

        def turn(label: str) -> dict[str, Any]:
            return stack.db.read(lambda u: u.get("turns", sessions[label]["turn_id"]))

        wait(
            lambda: all(turn(x)["state"] in ("succeeded", "failed", "cancelled") for x in labels),
            timeout=600,
        )
        intervals = []
        for label in labels:
            sid = sessions[label]["session_id"]
            events = stack.db.read(
                lambda u, sid=sid: u.find("session_events", {"session_id": sid}, order="seq")
            )
            by_type = {e["type"]: e for e in events}
            started = by_type.get("execution.started")
            ended = by_type.get("execution.observed_terminal")
            bound = by_type.get("executor.bound")
            session_row = stack.db.read(lambda u, sid=sid: u.get("sessions", sid))
            text = stack.db.read(
                lambda u, sid=sid: " ".join(
                    str(p["content"] or "")
                    for p in u.find("message_parts", {"session_id": sid}, order="ordinal")
                    if p["kind"] == "text"
                )
            )
            tools = [e for e in events if e["type"] == "tool.completed"]
            boot = BOOT_ID.search(text) or BOOT_ID.search(json.dumps([t["payload"] for t in tools]))
            usage = next((e["payload"] for e in events if e["type"] == "usage.observed"), {})
            lease = stack.db.read(
                lambda u, sid=sid: u.find_one("executor_leases", {"session_id": sid})
            )
            record["turns"][label] = {
                "state": turn(label)["state"],
                "model": session_row["harness_model"],
                "effort": session_row["harness_effort"],
                "inference_connection": session_row["inference_connection_id"],
                "mounted_slot": lease["machine_slot_id"] == slots[label]["id"],
                "runtime": (lease["handle"] or {}).get("runtime"),
                "boot_id": boot.group(0) if boot else None,
                "marker_returned": sessions[label]["marker"] in text,
                "shell_tool_calls": len(tools),
                "output_tokens": usage.get("output_tokens"),
                "input_tokens": usage.get("input_tokens"),
                "provisioning_ms": (bound["payload"].get("provisioning") if bound else None),
                "started_at": started["observed_at"].isoformat() if started else None,
                "ended_at": ended["observed_at"].isoformat() if ended else None,
            }
            if started and ended:
                intervals.append((started["observed_at"], ended["observed_at"]))
            if boot:
                boots.append(boot.group(0))
        overlap = (
            (min(e for _, e in intervals) - max(s for s, _ in intervals)).total_seconds()
            if len(intervals) == len(labels)
            else -1.0
        )
        record["overlap_seconds"] = round(overlap, 3)
        turns = record["turns"].values()
        gate(
            f"round{number}_{names}_all_turns_succeeded",
            all(t["state"] == "succeeded" for t in turns),
        )
        gate(f"round{number}_{names}_real_overlap", overlap > 0)
        gate(
            f"round{number}_{names}_exact_model_effort_and_real_usage",
            all(
                t["model"] == slots[label]["_choice"]["model"]
                and t["effort"] == slots[label]["_choice"]["effort"]
                and (t["output_tokens"] or 0) > 0
                and t["marker_returned"]
                and t["boot_id"]
                for label, t in record["turns"].items()
            ),
        )
        gate(
            f"round{number}_{names}_own_volume_vm_runtime_no_api_key",
            all(
                t["mounted_slot"] and t["runtime"] == "vm" and t["inference_connection"] is None
                for t in turns
            ),
        )
        busy = {label: view(slots[label]["id"])["status"] for label in labels}
        gate(
            f"round{number}_{names}_slots_show_running", all(v == "running" for v in busy.values())
        )
        for label in labels:
            user.post(f"/api/sessions/{sessions[label]['session_id']}/executor/releases")
        wait(lambda: all(view(slots[x]["id"])["status"] == "ready" for x in labels), timeout=240)
        gate(
            f"round{number}_{names}_slots_freed_no_stale_lock",
            all(not view(slots[x]["id"])["busy"] for x in labels),
        )

    REPORT["boot_ids"] = boots
    expected_boots = sum(len(r["slots"]) for r in REPORT["rounds"])
    gate(
        "every_worker_vm_had_its_own_fresh_boot",
        len(boots) == expected_boots and len(set(boots)) == expected_boots,
    )
    gate("no_vm_left_running", running_setup_vms(executor, user.workspace_id) == [])
    for label, slot in slots.items():
        user.delete(f"/api/machine-slots/{slot['id']}?confirm={label}")
    wait(lambda: user.get(slots_path).json()["items"] == [], timeout=180)
    gate(
        "volumes_preserved",
        all(volume_exists(executor, s["volume"]["name"]) for s in slots.values()),
    )
    leaked = [s for s in COMPUTE.values() if s in user.all_text()]
    gate("no_modal_token_in_api_responses", not leaked)


def _wait(condition: Any, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(1.0)
    raise AssertionError("condition not reached in time")


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="sbx-slots-par-"))
    admin, stop_pg = start_postgres(base)
    executor = ModalExecutor()
    REPORT.update(sdk=executor.sdk.__version__, runtime="vm")
    stop = threading.Event()
    try:
        db = Database(admin, max_connections=32)
        db.migrate()
        stack = ApiStack(db, base, executors={"modal": executor})
        workers = [
            threading.Thread(
                target=build_worker(stack.services, holder=f"acceptance-{i}").run_forever,
                args=(stop,),
                daemon=True,
            )
            for i in range(8)
        ]
        for worker in workers:
            worker.start()
        try:
            run(stack, executor)
        finally:
            stop.set()
            for worker in workers:
                worker.join(timeout=30)
            stack.shutdown()
    except BaseException as exc:
        REPORT["failure"] = type(exc).__name__ + ": " + str(exc)[:400]
        print("[FAIL]", REPORT["failure"], flush=True)
    finally:
        # A failed run must not leave Worker VMs behind (their own timeout is hours).
        try:
            client = executor._client(COMPUTE)
            for sandbox in executor.sdk.Sandbox.list(
                app_id=executor._app(client).app_id,
                tags={"sbx_workspace": REPORT.get("workspace_id", "none")},
                client=client,
            ):
                if sandbox.poll() is None:
                    sandbox.terminate()
                    REPORT.setdefault("swept", []).append(sandbox.object_id)
        except Exception as exc:
            REPORT["sweep_error"] = type(exc).__name__
        subprocess.run(stop_pg, capture_output=True)
        shutil.rmtree(base, ignore_errors=True)
    out = os.environ.get("SBX_SLOT_OUTPUT")
    if out:
        Path(out).write_text(json.dumps(REPORT, indent=2, default=str) + "\n")
    ok = "failure" not in REPORT and REPORT["gates"] and all(REPORT["gates"].values())
    print(
        "PARALLEL",
        "PASS" if ok else "FAIL",
        f"{sum(REPORT['gates'].values())}/{len(REPORT['gates'])}",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

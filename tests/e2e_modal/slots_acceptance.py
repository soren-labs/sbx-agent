"""Opt-in live acceptance for Machine Slots: real API, real PostgreSQL, real Modal Setup VMs.

Reads SBX_TEST_MODAL_TOKEN_ID/SECRET from the environment and never prints them. Not
collected by pytest. Nothing here reads a profile Volume: only the official CLI inside
the VM touches the login, and the script sees the supervisor's state through the API.

``SBX_TEST_SLOT_VOLUMES`` (``label=volume,...``) adopts Volumes that already hold a
user-approved official login and verifies each on a fresh VM. ``SBX_SLOT_LOGIN`` selects
what happens with one brand-new Slot:

- ``cancel`` (default): start the official device login, check the real URL/code/expiry,
  then cancel and delete; proves cleanup without asking anyone to approve.
- ``approve``: write the URL and code to ``SBX_SLOT_CODE_FILE`` and wait for a human to
  approve; then re-verify on another fresh VM and keep the Slot's Volume.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from control.executors.modal import APP_NAME, ModalExecutor  # noqa: E402
from control.persistence.database import Database  # noqa: E402

from tests.support.api import ApiStack, User  # noqa: E402
from tests.support.postgres import _free_port, _pg_bin  # noqa: E402

COMPUTE = {
    "token_id": os.environ["SBX_TEST_MODAL_TOKEN_ID"],
    "token_secret": os.environ["SBX_TEST_MODAL_TOKEN_SECRET"],
}
MODE = os.environ.get("SBX_SLOT_LOGIN", "cancel")
REPORT: dict[str, Any] = {"mode": MODE, "steps": {}, "gates": {}}


def gate(name: str, ok: Any) -> None:
    REPORT["gates"][name] = bool(ok)
    print(("[ok] " if ok else "[FAIL] ") + name, flush=True)


def start_postgres(base: Path) -> tuple[str, list[str]]:
    bindir = _pg_bin()
    data, sock, port = base / "pg", base / "sock", _free_port()
    sock.mkdir()
    subprocess.run(
        [str(bindir / "initdb"), "-D", str(data), "-U", "sbx", "--auth=trust", "-E", "UTF8"],
        check=True,
        capture_output=True,
    )
    options = f"-k {sock} -p {port} -c listen_addresses="
    subprocess.run(
        [
            str(bindir / "pg_ctl"),
            "-D",
            str(data),
            "-o",
            options,
            "-l",
            str(base / "pg.log"),
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    return f"host={sock} port={port} user=sbx dbname=postgres", [
        str(bindir / "pg_ctl"),
        "-D",
        str(data),
        "-m",
        "fast",
        "stop",
    ]


def running_setup_vms(executor: ModalExecutor, workspace_id: str) -> list[str]:
    client = executor._client(COMPUTE)
    app = executor._app(client)
    return [
        s.object_id
        for s in executor.sdk.Sandbox.list(
            app_id=app.app_id, tags={"sbx_workspace": workspace_id}, client=client
        )
        if s.poll() is None
    ]


def volume_exists(executor: ModalExecutor, name: str) -> bool:
    client = executor._client(COMPUTE)
    try:
        executor.sdk.Volume.from_name(name, client=client).hydrate()
    except executor.sdk.exception.NotFoundError:
        return False
    return True


def run(stack: ApiStack, executor: ModalExecutor) -> None:
    user = User(stack)
    modal = user.connect("modal", COMPUTE)
    stack.drain()
    slots = f"/api/workspaces/{user.workspace_id}/machine-slots"

    def view(slot_id: str) -> dict[str, Any]:
        return user.get(f"/api/machine-slots/{slot_id}").json()

    def settle(slot_id: str, timeout: float = 240) -> dict[str, Any]:
        stack.drive(lambda: view(slot_id)["status"] != "login_pending", timeout=timeout)
        return view(slot_id)

    adopted: dict[str, dict[str, Any]] = {}
    pairs = [p.split("=", 1) for p in os.environ.get("SBX_TEST_SLOT_VOLUMES", "").split(",") if p]
    for label, volume in pairs:
        started = time.time()
        slot = user.post(slots, {"provider": "codex", "label": label, "volume_name": volume}).json()
        done = settle(slot["id"])
        adopted[label] = done
        REPORT["steps"][f"adopt_{label}"] = {
            "status": done["status"],
            "login": done["login"]["state"],
            "error": done["login"]["error_code"],
            "capabilities": done["capabilities"],
            "seconds": round(time.time() - started, 1),
        }
        gate(
            f"adopted_{label}_ready_after_real_cli_call_on_fresh_vm",
            done["status"] == "ready"
            and done["capabilities"].get("verification", {}).get("real_model_call"),
        )
    if len(pairs) > 1:
        first = pairs[0][1]
        dup = user.post(slots, {"provider": "codex", "volume_name": first})
        gate("one_volume_never_backs_two_slots", dup.status_code == 422)

    started = time.time()
    fresh = user.post(slots, {"provider": "codex", "label": "Live login"}).json()
    stack.drive(lambda: view(fresh["id"])["login"]["state"] != "starting", timeout=240)
    waiting = view(fresh["id"])
    login = waiting["login"]
    REPORT["steps"]["device_login_started"] = {
        "state": login["state"],
        "verification_url": login["verification_url"],
        "user_code_shape": re.sub(r"[A-Z0-9]", "X", login["user_code"] or ""),
        "code_expires_at": login["code_expires_at"],
        "seconds_to_code": round(time.time() - started, 1),
        "volume": waiting["volume"],
    }
    gate(
        "official_url_and_code_returned",
        login["state"] == "awaiting_user"
        and (login["verification_url"] or "").startswith("https://auth.openai.com/")
        and bool(re.fullmatch(r"[A-Z0-9]{4,6}-[A-Z0-9]{4,6}", login["user_code"] or "")),
    )
    gate(
        "one_setup_vm_running_for_the_login",
        len(running_setup_vms(executor, user.workspace_id)) == 1,
    )
    busy = user.post(f"/api/machine-slots/{fresh['id']}/logins")
    gate("second_login_refused_while_one_is_live", busy.status_code == 409)

    if MODE == "approve":
        code_file = Path(os.environ["SBX_SLOT_CODE_FILE"])
        code_file.write_text(
            json.dumps({k: login[k] for k in ("verification_url", "user_code", "code_expires_at")})
        )
        code_file.chmod(0o600)
        print("AWAITING_APPROVAL", code_file, flush=True)
        done = settle(fresh["id"], timeout=1100)
        code_file.unlink(missing_ok=True)
        REPORT["steps"]["device_login_result"] = {
            "status": done["status"],
            "login": done["login"]["state"],
            "error": done["login"]["error_code"],
            "capabilities": done["capabilities"],
        }
        gate("approved_login_became_ready_automatically", done["status"] == "ready")
        gate("code_not_shown_after_use", done["login"]["user_code"] is None)
        if done["status"] == "ready":
            user.post(f"/api/machine-slots/{fresh['id']}/verifications")
            again = settle(fresh["id"])
            gate("login_survives_vm_destruction", again["status"] == "ready")
            REPORT["kept_slot_volume"] = done["volume"]["name"]
    else:
        cancelled = user.delete(f"/api/machine-slots/{fresh['id']}/logins/current")
        gate("cancel_accepted", cancelled.status_code == 202)
        after = settle(fresh["id"])
        gate(
            "cancelled_login_needs_login",
            after["login"]["state"] == "cancelled"
            and after["status"] == "needs_login"
            and after["login"]["user_code"] is None,
        )
        gate(
            "setup_vm_terminated_after_cancel", running_setup_vms(executor, user.workspace_id) == []
        )
        user.delete(f"/api/machine-slots/{fresh['id']}?confirm=Live login")
        stack.drive(
            lambda: user.get(f"/api/machine-slots/{fresh['id']}").status_code == 404, timeout=120
        )
        gate(
            "managed_volume_deleted_with_the_slot",
            not volume_exists(executor, fresh["volume"]["name"]),
        )

    for label, slot in adopted.items():
        user.delete(f"/api/machine-slots/{slot['id']}?confirm={label}")
        stack.drive(
            lambda s=slot: user.get(f"/api/machine-slots/{s['id']}").status_code == 404, timeout=120
        )
        gate(
            f"adopted_{label}_volume_preserved_on_delete",
            volume_exists(executor, slot["volume"]["name"]),
        )
    gate("no_setup_vm_left_running", running_setup_vms(executor, user.workspace_id) == [])
    leaked = [s for s in (COMPUTE["token_secret"], COMPUTE["token_id"]) if s in user.all_text()]
    rows = stack.db.read(lambda u: u.find("slot_login_attempts", {}))
    gate("no_modal_token_in_api_responses", not leaked)
    gate(
        "no_device_code_retained",
        all(r["user_code"] is None for r in rows)
        or MODE == "approve"
        and REPORT["gates"].get("approved_login_became_ready_automatically") is False,
    )
    REPORT["attempts"] = [
        {"mode": r["mode"], "state": r["state"], "error": r["error_code"]} for r in rows
    ]
    del modal


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="sbx-slots-"))
    admin, stop = start_postgres(base)
    executor = ModalExecutor()
    REPORT.update(sdk=executor.sdk.__version__, app=APP_NAME, runtime="vm")
    try:
        db = Database(admin)
        db.migrate()
        stack = ApiStack(db, base, executors={"modal": executor})
        try:
            run(stack, executor)
        finally:
            stack.shutdown()
    except BaseException as exc:
        REPORT["failure"] = type(exc).__name__ + ": " + str(exc)[:300]
        print("[FAIL]", REPORT["failure"], flush=True)
    finally:
        subprocess.run(stop, capture_output=True)
        shutil.rmtree(base, ignore_errors=True)
    out = os.environ.get("SBX_SLOT_OUTPUT")
    if out:
        Path(out).write_text(json.dumps(REPORT, indent=2) + "\n")
    ok = "failure" not in REPORT and REPORT["gates"] and all(REPORT["gates"].values())
    print(
        "SLOTS", "PASS" if ok else "FAIL", f"{sum(REPORT['gates'].values())}/{len(REPORT['gates'])}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

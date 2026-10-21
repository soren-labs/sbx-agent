"""Resident machines over real SSH (B2).

Real: PostgreSQL, Job worker, OpenSSH client + a private unprivileged sshd on 127.0.0.1,
the resident agent and the sbx-runtime daemon it starts. LABELLED FAKE: Turn content comes
from the repository's deterministic OpenCode stand-in (tests/fakes/official/opencode_cli.py)
pinned with the operator's OPENCODE_BIN override; no model, key or subscription is used.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from control.domain.errors import DomainError
from control.executors.resident import ResidentExecutor, Target
from tests.support import sshd as sshd_support
from tests.support.factories import make_principal
from tests.support.resident import ResidentStack

pytestmark = pytest.mark.skipif(
    not sshd_support.available(), reason="OpenSSH client/server binaries are not installed"
)


@pytest.fixture
def sshd(tmp_path):
    server = sshd_support.PrivateSshd(tmp_path / "sshd")
    server.start()
    yield server
    server.stop()


@pytest.fixture
def stack(db, tmp_path, sshd):
    s = ResidentStack(db, tmp_path, sshd)
    yield s
    s.shutdown()


def _environ(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    return dict(item.decode().split("=", 1) for item in raw if b"=" in item)


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rindex(")") + 2] not in "ZX"


def _turn(stack, principal, machine, text):
    created = stack.sessions.create(
        principal,
        principal.default_workspace_id,
        stack.body(machine["id"], message={"content": text}),
    )
    return created["session_id"], created["turn_id"]


def _ready(stack, session_id):
    def check():
        lease = stack.lease(session_id)
        worktree = stack.db.read(lambda u: u.find_one("worktrees", {"session_id": session_id}))
        return bool(lease and lease["state"] == "ready" and worktree["availability"] == "live")

    return check


# ------------------------------------------------------------------------------ discovery
def test_discovery_is_workspace_scoped_read_only_and_hides_the_ssh_target(stack) -> None:
    owner = make_principal(stack.db)
    machine = stack.register(owner)
    view = stack.machines.get(owner, machine["id"])
    assert view["reachability"]["status"] == "reachable", view
    assert view["reachability"]["runtime"]["digest_match"] is True
    assert view["capacity"] == {"max_leases": 4, "live_leases": 0, "unconfirmed_leases": 0}
    text = json.dumps(view)
    for secret_shaped in ("127.0.0.1", "identity", "known_hosts", "client_ed25519", "target"):
        assert secret_shaped not in text
    listed = stack.machines.list(owner, owner.default_workspace_id, probe=False)["items"]
    assert [m["id"] for m in listed] == [machine["id"]]
    assert listed[0]["reachability"]["status"] == "unchecked"
    stranger = make_principal(stack.db)
    with pytest.raises(DomainError) as missing:
        stack.machines.get(stranger, machine["id"])
    assert missing.value.code == "not_found"
    with pytest.raises(DomainError) as foreign:
        stack.sessions.create(stranger, stranger.default_workspace_id, stack.body(machine["id"]))
    assert foreign.value.code == "not_found"
    # Discovery wrote nothing: no lease, no job, no session.
    assert stack.db.read(lambda u: u.count("executor_leases")) == 0
    assert stack.db.read(lambda u: u.count("jobs")) == 0


def test_resident_session_requires_an_explicit_machine(stack) -> None:
    owner = make_principal(stack.db)
    with pytest.raises(DomainError) as missing:
        stack.sessions.create(
            owner,
            owner.default_workspace_id,
            {"harness": {"provider_id": "opencode"}, "executor": {"backend": "resident"}},
        )
    assert missing.value.code == "validation_failed"


# ------------------------------------------------------------------------- execution
def test_turn_runs_on_the_machine_with_its_own_dirs_and_no_ambient_home(stack) -> None:
    owner = make_principal(stack.db)
    machine = stack.register(owner)
    sid, turn_id = _turn(stack, owner, machine, "hello [write:a.txt=one]")
    stack.drive(stack.turn_done(turn_id), timeout=60)
    assert stack.turn(turn_id)["state"] == "succeeded"
    assert stack.output_text(turn_id).startswith("ACK: hello")
    lease = stack.lease(sid)
    handle = lease["handle"]
    assert lease["image_digest"].startswith("resident:")
    assert handle["endpoint"].startswith("resident://")
    lease_dir = Path(handle["lease_dir"])
    assert lease_dir.parent == stack.state_root / "leases"
    assert (lease_dir / "work").is_dir()
    env = _environ(handle["remote_pid"])
    assert env["HOME"] == str(lease_dir / "home")
    assert not set(env) - {
        "PATH",
        "HOME",
        "LANG",
        "PYTHONPATH",
        "SBX_RUNTIME_KEY",
        "SBX_LEASE_ID",
        "SBX_LEASE_GENERATION",
        "SBX_IMAGE_DIGEST",
        "OPENCODE_BIN",
    }, "only allow-listed variables reach the runtime"
    # A second job on the same machine gets its own lease, daemon and directories.
    sid2, turn2 = _turn(stack, owner, machine, "second [write:b.txt=two]")
    stack.drive(stack.turn_done(turn2), timeout=60)
    other = stack.lease(sid2)["handle"]
    assert other["lease_dir"] != handle["lease_dir"]
    assert other["remote_pid"] != handle["remote_pid"]
    assert stack.machines.get(owner, machine["id"], probe=False)["capacity"]["live_leases"] == 2


def test_file_and_terminal_command_task_through_the_live_api(stack) -> None:
    owner = make_principal(stack.db)
    machine = stack.register(owner)
    sid = stack.sessions.create(owner, owner.default_workspace_id, stack.body(machine["id"]))[
        "session_id"
    ]
    stack.execution.activate(owner, sid)
    stack.drive(_ready(stack, sid), timeout=60)
    stack.live.write_file(
        owner,
        sid,
        {"path": "task.txt", "content": "payload-42\n", "expected_digest": "absent"},
        idempotency_key="w1",
    )
    term = stack.live.create_terminal(owner, sid, idempotency_key="t1")["terminal_id"]
    stack.live.terminal_input(
        owner,
        sid,
        term,
        "cat task.txt; pwd > where.txt; echo DONE-$((6*7))\n",
        idempotency_key="i1",
    )
    deadline = time.monotonic() + 20
    text = ""
    while time.monotonic() < deadline and "DONE-42" not in text:
        text = stack.live.terminal_output(owner, sid, term, 0).get("data", "")
        time.sleep(0.2)
    assert "payload-42" in text and "DONE-42" in text
    where = stack.live.read_file(owner, sid, "where.txt")["content"].strip()
    assert where.startswith(str(stack.lease(sid)["handle"]["lease_dir"]))


def test_allocation_retry_adopts_the_same_daemon(stack, sshd) -> None:
    executor = stack.resident
    compute = {"target": sshd.target(stack.state_root), "resident_machine_id": None}
    spec = {
        "lease_id": "lease_adopt",
        "generation": 1,
        "enrollment_key": "ab" * 32,
        "compute": compute,
    }
    first = executor.allocate(spec, "op_adopt")
    second = executor.allocate(spec, "op_adopt")  # e.g. the first response was lost
    assert first["remote_pid"] == second["remote_pid"] and first["status"] == "running"
    assert executor.lookup("op_adopt", compute)["remote_pid"] == first["remote_pid"]
    daemons = subprocess.run(
        ["pgrep", "-f", str(stack.state_root / "leases" / "lease_adopt" / "state")],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert daemons == [str(first["remote_pid"])]
    assert executor.terminate(first, "t", compute) is True
    assert not _alive(first["remote_pid"])
    assert executor.lookup("op_adopt", compute)["status"] == "terminated"
    assert executor.lookup("op_never", compute) is None


def test_control_plane_restart_reconnects_to_the_same_runtime(stack) -> None:
    owner = make_principal(stack.db)
    machine = stack.register(owner)
    sid, turn_id = _turn(stack, owner, machine, "survive [slow:4] [write:s.txt=kept]")
    stack.drive(lambda: stack.turn(turn_id)["state"] == "running", timeout=60)
    before = stack.lease(sid)
    # The control plane goes away: its SSH tunnels die; the machine keeps running.
    stack.resident.tunnels.close_all()
    stack.new_executor()
    stack.rebuild_worker()
    assert _alive(before["handle"]["remote_pid"])
    stack.drive(stack.turn_done(turn_id), timeout=60)
    after = stack.lease(sid)
    assert stack.turn(turn_id)["state"] == "succeeded"
    assert after["id"] == before["id"] and after["runtime_epoch"] == before["runtime_epoch"]
    assert after["handle"]["remote_pid"] == before["handle"]["remote_pid"]


def test_unreachable_machine_is_reported_never_succeeded_and_resumes(stack, sshd) -> None:
    owner = make_principal(stack.db)
    machine = stack.register(owner)
    sid, turn_id = _turn(stack, owner, machine, "offline [slow:6] [write:o.txt=after]")
    stack.drive(lambda: stack.turn(turn_id)["state"] == "running", timeout=60)
    lease = stack.lease(sid)
    sshd.stop()  # the link is down: no SSH, no tunnel
    stack.resident.tunnels.close_all()
    stack.drive(lambda: stack.lease(sid)["observed_status"] == "unreachable", timeout=60)
    assert stack.turn(turn_id)["state"] == "running", "unknown is not finished"
    types = [e["type"] for e in stack.events(sid)]
    assert "executor.unreachable" in types and "turn.succeeded" not in types
    with pytest.raises(DomainError) as unavailable:
        stack.live.list_files(owner, sid)
    assert unavailable.value.code == "executor_unavailable"
    assert stack.resident.probe(machine["target"], fresh=True)["status"] == "unreachable"
    assert _alive(lease["handle"]["remote_pid"]), "the machine's runtime keeps running"
    sshd.start()
    stack.drive(stack.turn_done(turn_id), timeout=90)
    assert stack.turn(turn_id)["state"] == "succeeded"
    after = stack.lease(sid)
    assert after["runtime_epoch"] == lease["runtime_epoch"] and after["observed_status"] == "ready"
    assert "executor.reachable" in [e["type"] for e in stack.events(sid)]


def test_grace_expiry_is_outcome_unknown_and_cleanup_waits_for_confirmation(
    db, tmp_path, sshd
) -> None:
    stack = ResidentStack(db, tmp_path, sshd, grace=2.0)
    try:
        owner = make_principal(stack.db)
        machine = stack.register(owner)
        sid, turn_id = _turn(stack, owner, machine, "lost [hang]")
        stack.drive(lambda: stack.turn(turn_id)["state"] == "running", timeout=60)
        lease = stack.lease(sid)
        sshd.stop()
        stack.resident.tunnels.close_all()
        stack.drive(stack.turn_done(turn_id), timeout=90)
        turn = stack.turn(turn_id)
        assert turn["state"] == "interrupted" and turn["reason"] == "outcome_unknown"
        lost = stack.lease(sid)
        assert lost["state"] == "lost" and lost["quarantined"] is True
        assert _alive(lease["handle"]["remote_pid"]), "unconfirmed: nothing was claimed stopped"
        sshd.start()
        stack.drive(lambda: not stack.lease(sid)["quarantined"], timeout=90)
        assert not _alive(lease["handle"]["remote_pid"])
    finally:
        stack.shutdown()


def test_cancel_and_release_stop_exactly_the_owned_processes(stack) -> None:
    owner = make_principal(stack.db)
    machine = stack.register(owner)
    sid, turn_id = _turn(stack, owner, machine, "block [hang]")
    stack.drive(lambda: stack.turn(turn_id)["state"] == "running", timeout=60)
    stack.sessions.cancel_turn(owner, turn_id)
    stack.drive(stack.turn_done(turn_id), timeout=60)
    assert stack.turn(turn_id)["state"] == "cancelled"
    # A bystander process that merely looks related is never signalled.
    bystander = subprocess.Popen(["sleep", "300"], start_new_session=True)
    term = stack.live.create_terminal(owner, sid, idempotency_key="t")["terminal_id"]
    stack.live.terminal_input(
        owner, sid, term, "( exec -a marker-b2 sleep 900 )\n", idempotency_key="i"
    )
    deadline = time.monotonic() + 10
    while (
        time.monotonic() < deadline
        and not subprocess.run(["pgrep", "-f", "^marker-b2"], capture_output=True).stdout
    ):
        time.sleep(0.1)
    assert subprocess.run(["pgrep", "-f", "^marker-b2"], capture_output=True).stdout
    lease = stack.lease(sid)
    stack.execution.release(owner, sid)
    stack.drive(lambda: stack.lease(sid)["state"] == "released", timeout=90)
    assert not _alive(lease["handle"]["remote_pid"])
    leftovers = subprocess.run(["pgrep", "-f", "^marker-b2"], capture_output=True, text=True)
    assert leftovers.stdout.strip() == ""
    assert _alive(bystander.pid)
    bystander.kill()
    bystander.wait()


def test_terminate_never_signals_a_process_that_is_not_the_recorded_one(stack, sshd) -> None:
    """A registry record whose pid now belongs to another process (recycled pid)."""
    compute = {"target": sshd.target(stack.state_root), "resident_machine_id": None}
    spec = {"lease_id": "lease_r", "generation": 1, "enrollment_key": "cd" * 32, "compute": compute}
    handle = stack.resident.allocate(spec, "op_recycled")
    stranger = subprocess.Popen(["sleep", "300"], start_new_session=True)
    os.kill(handle["remote_pid"], signal.SIGKILL)
    time.sleep(0.3)
    record_path = stack.state_root / "registry" / "op_recycled.json"
    record = json.loads(record_path.read_text())
    record["pid"] = stranger.pid  # simulate pid reuse: same number, different process
    record_path.write_text(json.dumps(record))
    assert stack.resident.lookup("op_recycled", compute)["status"] == "terminated"
    assert stack.resident.terminate(handle, "t", compute) is True
    assert _alive(stranger.pid)
    stranger.kill()
    stranger.wait()


def test_capacity_limit_makes_further_sessions_wait(stack) -> None:
    owner = make_principal(stack.db)
    machine = stack.register(owner, max_leases=1)
    sid1, t1 = _turn(stack, owner, machine, "first [hang]")
    stack.drive(lambda: stack.turn(t1)["state"] == "running", timeout=60)
    sid2, t2 = _turn(stack, owner, machine, "second")
    stack.drive(lambda: stack.turn(t2)["reason"] == "waiting_capacity", timeout=30)
    assert stack.lease(sid2) is None
    stack.sessions.cancel_turn(owner, t1)
    stack.drive(stack.turn_done(t1), timeout=60)
    stack.execution.release(owner, sid1)
    stack.drive(stack.turn_done(t2), timeout=90)
    assert stack.turn(t2)["state"] == "succeeded"


# ------------------------------------------------------------------- failure categories
def test_offline_machine_never_reports_success(stack, sshd) -> None:
    owner = make_principal(stack.db)
    closed = sshd_support._free_port()
    machine = stack.register(owner, port=closed)
    assert stack.machines.get(owner, machine["id"])["reachability"]["status"] == "unreachable"
    sid, turn_id = _turn(stack, owner, machine, "never [write:x.txt=x]")
    stack.drive(
        lambda: (
            stack.db.read(
                lambda u: u.find_one("jobs", {"kind": "turn.dispatch", "turn_id": turn_id})
            )["last_error_code"]
            == "executor_unavailable"
        ),
        timeout=60,
    )
    assert stack.turn(turn_id)["state"] == "preparing"
    stack.sessions.cancel_turn(owner, turn_id)
    stack.drive(stack.turn_done(turn_id), timeout=60)
    assert stack.turn(turn_id)["state"] == "cancelled"
    assert "turn.succeeded" not in [e["type"] for e in stack.events(sid)]


def test_unknown_host_key_is_refused_without_trust_on_first_use(stack, sshd, tmp_path) -> None:
    other = tmp_path / "other_host"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    wrong = tmp_path / "wrong_known_hosts"
    key = (tmp_path / "other_host.pub").read_text().split()
    wrong.write_text(f"[127.0.0.1]:{sshd.port} {key[0]} {key[1]}\n")
    target = sshd.target(stack.state_root, known_hosts_file=str(wrong))
    assert stack.resident.probe(target, fresh=True)["status"] == "host_key_unverified"
    assert wrong.read_text().count("\n") == 1, "known_hosts is never rewritten"
    with pytest.raises(DomainError) as refused:
        stack.resident.lookup("op_x", {"target": target})
    assert refused.value.code == "executor_unavailable" and refused.value.retryable


def test_runtime_version_mismatch_is_refused(stack, sshd) -> None:
    executor = ResidentExecutor(expected_digest="0" * 32)
    compute = {"target": sshd.target(stack.state_root)}
    assert executor.probe(compute["target"], fresh=True)["status"] == "runtime_mismatch"
    with pytest.raises(DomainError) as refused:
        executor.allocate(
            {
                "lease_id": "lease_v",
                "generation": 1,
                "enrollment_key": "ef" * 32,
                "compute": compute,
            },
            "op_version",
        )
    assert refused.value.code == "runtime_incompatible"
    assert not (stack.state_root / "leases" / "lease_v").exists()
    assert Target.from_dict(compute["target"]).port == sshd.port


def test_unconfirmed_lost_runtime_keeps_its_capacity_until_termination_is_confirmed(
    db, tmp_path, sshd
) -> None:
    """Review regression: a lost-but-quarantined lease still occupies the machine."""
    stack = ResidentStack(db, tmp_path, sshd, grace=2.0)
    try:
        owner = make_principal(stack.db)
        machine = stack.register(owner, max_leases=1)
        sid1, t1 = _turn(stack, owner, machine, "first [hang]")
        stack.drive(lambda: stack.turn(t1)["state"] == "running", timeout=60)
        first = stack.lease(sid1)
        sshd.stop()
        stack.resident.tunnels.close_all()
        stack.drive(stack.turn_done(t1), timeout=90)
        lost = stack.lease(sid1)
        assert lost["state"] == "lost" and lost["quarantined"] is True
        capacity = stack.machines.get(owner, machine["id"], probe=False)["capacity"]
        assert capacity == {"max_leases": 1, "live_leases": 1, "unconfirmed_leases": 1}
        sid2, t2 = _turn(stack, owner, machine, "second [write:two.txt=2]")
        stack.drive(lambda: stack.turn(t2)["reason"] == "waiting_capacity", timeout=30)
        assert stack.lease(sid2) is None, "no second runtime while the first is unconfirmed"
        assert _alive(first["handle"]["remote_pid"])
        sshd.start()
        stack.drive(stack.turn_done(t2), timeout=180)
        assert stack.turn(t2)["state"] == "succeeded"
        assert not _alive(first["handle"]["remote_pid"])
        assert stack.lease(sid1)["quarantined"] is False
        second = stack.lease(sid2)
        assert second["handle"]["remote_pid"] != first["handle"]["remote_pid"]
    finally:
        stack.shutdown()


def test_ssh_forwards_never_outlive_the_control_plane_process(sshd, tmp_path) -> None:
    """Regression: uvicorn re-raises SIGTERM (no atexit) and SIGKILL skips cleanup; the
    forward's watchdog still stops ssh when the owning process dies."""
    target = json.dumps(sshd.target(tmp_path / "remote"))
    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import json,sys,time\n"
            "from control.executors.resident import Target, Tunnels\n"
            "t = Target.from_dict(json.loads(sys.argv[1]))\n"
            "print(Tunnels().ensure('lease_x', t.port, lambda: t), flush=True)\n"
            "time.sleep(600)\n",
            target,
        ],
        stdout=subprocess.PIPE,
        text=True,
        cwd=str(sshd_support.REPO_ROOT),
    )
    port = int(owner.stdout.readline())
    pattern = f"127.0.0.1:{port}:127.0.0.1:{sshd.port}"
    assert subprocess.run(["pgrep", "-f", pattern], capture_output=True).stdout
    owner.kill()
    owner.wait()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not subprocess.run(["pgrep", "-f", pattern], capture_output=True).stdout:
            break
        time.sleep(0.1)
    assert not subprocess.run(["pgrep", "-f", pattern], capture_output=True).stdout

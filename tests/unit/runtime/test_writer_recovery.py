"""Detached writers retain attribution and cleanup obligations across restart."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest
from runtime.daemon import app, changes
from runtime.daemon.supervisor import _alive, kill_group, managed_popen
from tests.support.runtime import FAKE_OPENCODE
from tests.unit.runtime import test_quiescence
from tests.unit.runtime.test_quiescence import wait_file, writer

rt = test_quiescence.rt


def detached_writer(tmp_path, root, mode="clean"):
    child, ready, release, changed = writer(tmp_path, root)
    if mode == "undumpable":
        child.write_text(
            "import ctypes\nassert ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0\n"
            + child.read_text()
        )
    parent = tmp_path / "launch.py"
    parent.write_text(
        "import subprocess, sys, pathlib, time, runpy\n"
        f"subprocess.Popen([sys.executable, {str(child)!r}], start_new_session=True, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "env={'PATH': '/usr/bin:/bin'})\n"
        f"while not pathlib.Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
    )
    return parent, ready, release, changed


@pytest.mark.parametrize("mode", ["clean", "undumpable"])
def test_scope_kills_detached_child_without_reading_environment(tmp_path, mode):
    parent, ready, release, changed = detached_writer(tmp_path, tmp_path, mode)
    proc = managed_popen(
        [sys.executable, str(parent)], env={"PATH": "/usr/bin:/bin"}, start_new_session=True
    )
    pid = int(wait_file(ready))
    try:
        assert proc.wait(timeout=10) == 0
        assert _alive(pid)
        if mode == "undumpable":
            with pytest.raises(PermissionError):
                Path(f"/proc/{pid}/environ").read_bytes()
        assert kill_group(proc.pid, grace=0.1)
        assert not _alive(pid)
        release.touch()
        assert not changed.exists()
    finally:
        kill_group(proc.pid, grace=0.1)
        if _alive(pid):
            os.kill(pid, 9)


def test_unreadable_process_state_cannot_confirm_stop(tmp_path, monkeypatch):
    parent, ready, _, _ = detached_writer(tmp_path, tmp_path)
    proc = managed_popen(
        [sys.executable, str(parent)], env={"PATH": "/usr/bin:/bin"}, start_new_session=True
    )
    pid = int(wait_file(ready))
    proc.wait(timeout=10)
    original = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == Path(f"/proc/{pid}/stat"):
            raise PermissionError("process state unavailable")
        return original(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", unreadable)
            assert kill_group(proc.pid, grace=0.1) is False
            assert _alive(pid)
        assert kill_group(proc.pid, grace=0.1)
        assert not _alive(pid)
    finally:
        kill_group(proc.pid, grace=0.1)


def test_escalation_keeps_anchor_until_detached_child_is_dead(tmp_path):
    parent, ready, release, changed = detached_writer(tmp_path, tmp_path)
    child = tmp_path / "writer.py"
    child.write_text(
        "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n" + child.read_text()
    )
    proc = managed_popen(
        [sys.executable, str(parent)], env={"PATH": "/usr/bin:/bin"}, start_new_session=True
    )
    pid = int(wait_file(ready))
    proc.wait(timeout=10)
    try:
        assert kill_group(proc.pid, grace=0.1)
        assert not _alive(pid)
        release.touch()
        assert not changed.exists()
    finally:
        kill_group(proc.pid, grace=0.1)


def test_scope_survives_launching_daemon_process_death(tmp_path, repo_root):
    parent, ready, release, changed = detached_writer(tmp_path, tmp_path)
    driver = tmp_path / "driver.py"
    anchor_pid = tmp_path / "anchor-pid"
    driver.write_text(
        "import os, sys, subprocess\nfrom pathlib import Path\n"
        "from runtime.daemon.supervisor import managed_popen\n"
        f"proc = managed_popen([sys.executable, {str(parent)!r}], "
        "env={'PATH': '/usr/bin:/bin'}, scope='daemon-crash', start_new_session=True, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"Path({str(anchor_pid)!r}).write_text(str(proc.pid))\n"
        "os._exit(0)\n"
    )
    subprocess.run(
        [sys.executable, str(driver)],
        check=True,
        timeout=10,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(repo_root)},
    )
    pid, anchor = int(wait_file(ready)), int(wait_file(anchor_pid))
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            # Wait for adoption after the managed parent exits with no status reader.
            if int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1]) == anchor:
                break
            time.sleep(0.01)
        else:
            pytest.fail("detached child lost its anchor after daemon death")
        assert kill_group(0, grace=0.1, scope="daemon-crash")
        assert not _alive(pid)
        release.touch()
        assert not changed.exists()
    finally:
        kill_group(0, grace=0.1, scope="daemon-crash")
        if _alive(pid):
            os.kill(pid, 9)


@pytest.mark.parametrize("mode", ["clean", "undumpable"])
@pytest.mark.parametrize("surface", ["turn", "service", "terminal"])
def test_stop_success_requires_detached_writer_death(rt, tmp_path, monkeypatch, mode, surface):
    parent, ready, release, changed = detached_writer(tmp_path, rt.daemon.worktree.path, mode)
    pid = None
    try:
        if surface == "turn":
            parent.write_text(
                parent.read_text() + f"runpy.run_path({FAKE_OPENCODE!r}, run_name='__main__')\n"
            )
            monkeypatch.setenv("OPENCODE_BIN", str(parent))
            response = rt.op(
                "turn.start",
                "turn",
                {
                    "provider_id": "opencode",
                    "turn_id": "turn",
                    "execution_id": "execution",
                    "prompt": "hello",
                    "deadline_seconds": 10,
                },
                secrets_={"opencode_zen": {"api_key": "REDACTED"}},
            )
            assert response.status_code == 200
            pid = int(wait_file(ready))
            result = rt.wait_op("turn")
            assert rt.events()[-1]["payload"]["stopped"] is True
        elif surface == "service":
            assert (
                rt.op(
                    "service.ensure",
                    "service",
                    {
                        "declaration": {
                            "name": "writer",
                            "argv": [sys.executable, str(parent)],
                            "restart": "never",
                        }
                    },
                ).json()["status"]
                == "succeeded"
            )
            pid = int(wait_file(ready))
            result = rt.op("service.stop", "stop", {"name": "writer"}).json()
        else:
            term = rt.op("terminal.create", "terminal", {}).json()["result"]["terminal_id"]
            rt.op(
                "terminal.input",
                "input",
                {
                    "terminal_id": term,
                    "data": shlex.join([sys.executable, str(parent)]) + "\n",
                },
            )
            pid = int(wait_file(ready))
            result = rt.op("terminal.close", "stop", {"terminal_id": term}).json()
        assert result["status"] == "succeeded", result
        assert not _alive(pid)
        release.touch()
        assert not changed.exists()
    finally:
        if pid and _alive(pid):
            os.kill(pid, 9)


@pytest.mark.parametrize("kind", ["changes.capture", "snapshot.prepare"])
@pytest.mark.parametrize("record_pid", [False, True])
def test_failed_turn_recovery_blocks_sealing_until_confirmed(
    rt, tmp_path, monkeypatch, kind, record_pid
):
    parent, ready, release, changed = detached_writer(tmp_path, rt.daemon.worktree.path)
    operation = "interrupted-turn"
    rt.daemon.journal.op_insert(operation, "turn.start", "digest", "sess_test", 1)
    proc = managed_popen(
        [sys.executable, str(parent)],
        env={"PATH": "/usr/bin:/bin"},
        scope=operation,
        start_new_session=True,
    )
    pid = int(wait_file(ready))
    proc.wait(timeout=10)
    rt.daemon.journal.op_update(operation, status="starting")
    if record_pid:
        rt.daemon.journal.op_update(operation, status="started", pid=proc.pid)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(app, "kill_group", lambda *a, **kw: False)
            rt.stop()
            rt.start()
            assert rt.daemon.journal.op_get(operation)["result"]["stopped"] is False
            # Repeat restart after terminalization: the obligation must remain durable.
            rt.stop()
            rt.start()
            assert _alive(pid)
            target, method = (
                (changes, "capture")
                if kind == "changes.capture"
                else (rt.daemon.worktree, "checkpoint")
            )
            patch.setattr(target, method, lambda *a, **kw: pytest.fail("sealed a surviving writer"))
            result = rt.op(
                kind, "blocked", {"base_sha": rt.base_sha, "require_acked": False}
            ).json()
            assert result["status"] == "failed" and result["result"]["error"]["code"] == "busy"
            assert (
                rt.op(
                    "files.write", "blocked-write", {"path": "late.txt", "content": "late"}
                ).json()["status"]
                == "failed"
            )
            assert (
                rt.op("turn.cancel", "cancel", {"target_operation_id": operation}).json()["result"][
                    "stopped"
                ]
                is False
            )
        result = rt.op(kind, "confirmed", {"base_sha": rt.base_sha, "require_acked": False}).json()
        assert result["status"] == "succeeded", result
        assert not _alive(pid)
        release.touch()
        assert not changed.exists()
    finally:
        kill_group(proc.pid, grace=0.1, scope=operation)
        if _alive(pid):
            os.kill(pid, 9)


def test_recovery_finds_turn_scope_before_pid_journaling(rt, tmp_path):
    parent, ready, _, _ = detached_writer(tmp_path, rt.daemon.worktree.path)
    rt.daemon.journal.op_insert("unrecorded-pid", "turn.start", "digest", "sess_test", 1)
    proc = managed_popen(
        [sys.executable, str(parent)],
        env={"PATH": "/usr/bin:/bin"},
        scope="unrecorded-pid",
        start_new_session=True,
    )
    pid = int(wait_file(ready))
    proc.wait(timeout=10)
    try:
        rt.stop()
        rt.start()
        assert rt.daemon.journal.op_get("unrecorded-pid")["result"]["stopped"] is True
        assert not _alive(pid)
    finally:
        kill_group(proc.pid, grace=0.1)


@pytest.mark.parametrize("kind", ["changes.capture", "snapshot.prepare"])
@pytest.mark.parametrize("writer_kind", ["service", "terminal"])
def test_restart_preserves_successful_writer_cleanup(rt, tmp_path, monkeypatch, kind, writer_kind):
    parent, ready, release, changed = detached_writer(tmp_path, rt.daemon.worktree.path)
    if writer_kind == "service":
        assert (
            rt.op(
                "service.ensure",
                "writer",
                {
                    "declaration": {
                        "name": "writer",
                        "argv": [sys.executable, str(parent)],
                        "restart": "never",
                    }
                },
            ).json()["status"]
            == "succeeded"
        )
        close_kind, close_payload = "service.stop", {"name": "writer"}
    else:
        term = rt.op("terminal.create", "writer", {}).json()["result"]["terminal_id"]
        rt.op(
            "terminal.input",
            "input",
            {
                "terminal_id": term,
                "data": shlex.join([sys.executable, str(parent)]) + "\n",
            },
        )
        close_kind, close_payload = "terminal.close", {"terminal_id": term}
    pid = int(wait_file(ready))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(app, "kill_group", lambda *a, **kw: False)
            rt.stop()
            rt.start()
            assert _alive(pid)
            assert (
                rt.op(close_kind, "unconfirmed-close", close_payload).json()["status"] == "failed"
            )
            assert (
                rt.op(kind, "blocked", {"base_sha": rt.base_sha, "require_acked": False}).json()[
                    "result"
                ]["error"]["code"]
                == "busy"
            )
        target, method = (
            (changes, "capture")
            if kind == "changes.capture"
            else (rt.daemon.worktree, "checkpoint")
        )
        original = getattr(target, method)

        def seal(*args, **kwargs):
            assert not _alive(pid)
            release.touch()
            return original(*args, **kwargs)

        monkeypatch.setattr(target, method, seal)
        assert (
            rt.op(kind, "confirmed", {"base_sha": rt.base_sha, "require_acked": False}).json()[
                "status"
            ]
            == "succeeded"
        )
        assert rt.op(close_kind, "confirmed-close", close_payload).json()["status"] == "succeeded"
        assert not changed.exists()
    finally:
        kill_group(0, grace=0.1, scope="writer")
        if _alive(pid):
            os.kill(pid, 9)

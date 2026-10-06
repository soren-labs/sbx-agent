"""Managed writers must stop before Turn completion, capture and checkpoint."""

from __future__ import annotations

import shlex
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from runtime.daemon import changes, services
from runtime.daemon.services import Service
from runtime.daemon.supervisor import _alive, kill_group
from tests.support.runtime import DaemonHarness


@pytest.fixture
def rt(tmp_path):
    harness = DaemonHarness(tmp_path / "runtime")
    restored = harness.op("worktree.restore", "restore", {}).json()
    harness.base_sha = restored["result"]["base_sha"]
    yield harness
    for term in harness.daemon.terminals.values():
        term.close()
    for service in harness.daemon.services.values():
        service.stop()
    harness.stop()


def wait_file(path):
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), path
    return path.read_text()


def writer(tmp_path, root):
    script = tmp_path / "writer.py"
    ready, release, changed = tmp_path / "ready", tmp_path / "release", root / "changed.txt"
    script.write_text(
        "import os, pathlib, time\n"
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
        f"while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        f"pathlib.Path({str(changed)!r}).write_text('post-success mutation')\n"
    )
    return script, ready, release, changed


@pytest.mark.parametrize("inherit_pipes", [False, True])
def test_successful_cli_stops_descendant_before_terminal_evidence(
    rt, tmp_path, monkeypatch, inherit_pipes
):
    script, ready, release, changed = writer(tmp_path, rt.daemon.worktree.path)
    wrapper = tmp_path / "cli.py"
    from tests.support.runtime import FAKE_OPENCODE

    wrapper.write_text(
        "import pathlib, runpy, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(script)!r}], "
        + ("" if inherit_pipes else "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,")
        + ")\n"
        f"while not pathlib.Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
        f"runpy.run_path({FAKE_OPENCODE!r}, run_name='__main__')\n"
    )
    monkeypatch.setenv("OPENCODE_BIN", str(wrapper))
    rt.op(
        "turn.start",
        "turn",
        {
            "provider_id": "opencode",
            "turn_id": "turn",
            "execution_id": "exec",
            "prompt": "hello",
            "deadline_seconds": 10,
        },
        secrets_={"opencode_zen": {"api_key": "REDACTED"}},
    )
    pid = int(wait_file(ready))
    try:
        status = rt.wait_op("turn")
        assert status["status"] == "succeeded", status
        assert not _alive(pid), "a completed Turn must not leave a descendant writer"
        assert rt.events()[-1]["payload"]["stopped"] is True
        release.touch()
        assert not changed.exists()
    finally:
        kill_group(rt.daemon.runs["turn"].proc.pid, grace=0.1)


@pytest.mark.parametrize("kind", ["changes.capture", "snapshot.prepare"])
@pytest.mark.parametrize("writer_kind", ["terminal", "service"])
def test_artifacts_stop_background_writers(rt, tmp_path, monkeypatch, kind, writer_kind):
    script, ready, release, changed = writer(tmp_path, rt.daemon.worktree.path)
    if writer_kind == "terminal":
        term_id = rt.op("terminal.create", "terminal", {}).json()["result"]["terminal_id"]
        command = shlex.join([sys.executable, str(script)]) + " &\n"
        rt.op("terminal.input", "input", {"terminal_id": term_id, "data": command})
    else:
        wrapper = tmp_path / "service.py"
        wrapper.write_text(
            "import pathlib, subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, {str(script)!r}], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"while not pathlib.Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
        )
        rt.op(
            "service.ensure",
            "service",
            {
                "declaration": {
                    "name": "writer",
                    "argv": [sys.executable, str(wrapper)],
                    "restart": "never",
                }
            },
        )
        rt.daemon.services["writer"].proc.wait(timeout=10)
    pid = int(wait_file(ready))
    assert _alive(pid)
    if kind == "changes.capture":
        original = changes.capture

        def capture(*args, **kwargs):
            assert not _alive(pid), "capture must wait for the terminal's background job"
            release.touch()
            return original(*args, **kwargs)

        monkeypatch.setattr(changes, "capture", capture)
    else:
        original = rt.daemon.worktree.checkpoint

        def checkpoint(*args, **kwargs):
            assert not _alive(pid), "checkpoint must wait for the terminal's background job"
            release.touch()
            return original(*args, **kwargs)

        monkeypatch.setattr(rt.daemon.worktree, "checkpoint", checkpoint)
    result = rt.op(kind, "artifact", {"base_sha": rt.base_sha}).json()
    assert result["status"] == "succeeded", result
    assert not changed.exists()
    if writer_kind == "terminal":
        assert rt.daemon.terminals[term_id].read(0)["closed"] is True
        refused = rt.op(
            "terminal.input", "late-input", {"terminal_id": term_id, "data": "echo late\n"}
        ).json()
        assert refused["status"] == "failed"


@pytest.mark.parametrize("kind", ["changes.capture", "snapshot.prepare"])
@pytest.mark.parametrize("writer_kind", ["terminal", "service", "cli"])
def test_artifacts_refuse_unconfirmed_writer_stop(rt, monkeypatch, kind, writer_kind):
    if writer_kind == "terminal":
        term_id = rt.op("terminal.create", "terminal", {}).json()["result"]["terminal_id"]
        monkeypatch.setattr(rt.daemon.terminals[term_id], "close", lambda: False)
    elif writer_kind == "service":
        rt.daemon.services["writer"] = SimpleNamespace(stop=lambda: False)
    else:
        done = threading.Event()
        done.set()
        rt.daemon.runs["old-cli"] = SimpleNamespace(done=done, stop_confirmed=False)
    result = rt.op(kind, "artifact", {"base_sha": rt.base_sha}).json()
    assert result["status"] == "failed" and result["result"]["error"]["code"] == "busy"


def test_service_pending_restart_is_revoked_by_stop(tmp_path, monkeypatch):
    waiting, release, attempted = threading.Event(), threading.Event(), threading.Event()

    def delay(_):
        waiting.set()
        assert release.wait(10)

    monkeypatch.setattr(services, "time", SimpleNamespace(sleep=delay))
    service = Service(
        {"name": "writer", "argv": [sys.executable, "-c", "raise SystemExit(1)"]},
        tmp_path,
        tmp_path / "service-home",
    )
    original = service.start

    def start(**kwargs):
        original(**kwargs)
        if kwargs.get("restart"):
            attempted.set()

    monkeypatch.setattr(service, "start", start)
    try:
        service.start()
        assert waiting.wait(10)
        proc = service.proc
        assert service.stop()
        release.set()
        assert attempted.wait(10)
        assert service.proc is proc and not service.wanted
    finally:
        release.set()
        service.stop()


@pytest.mark.parametrize("writer_kind", ["terminal", "service"])
def test_unconfirmed_stop_keeps_writer_managed(rt, monkeypatch, writer_kind):
    if writer_kind == "terminal":
        term_id = rt.op("terminal.create", "terminal", {}).json()["result"]["terminal_id"]
        writer = rt.daemon.terminals[term_id]
        monkeypatch.setattr(writer, "close", lambda: False)
        result = rt.op("terminal.close", "close", {"terminal_id": term_id}).json()
        assert rt.daemon.terminals[term_id] is writer
    else:
        writer = SimpleNamespace(decl={"name": "writer", "argv": ["old"]}, stop=lambda: False)
        rt.daemon.services["writer"] = writer
        result = rt.op(
            "service.ensure", "replace", {"declaration": {"name": "writer", "argv": ["new"]}}
        ).json()
        assert rt.daemon.services["writer"] is writer
    assert result["status"] == "failed" and result["result"]["error"]["code"] == "busy"
    artifact = rt.op("snapshot.prepare", "artifact", {}).json()
    assert artifact["status"] == "failed" and artifact["result"]["error"]["code"] == "busy"

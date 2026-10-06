"""Crash recovery accounts for every launch and proves dead startup absence."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from runtime.daemon import changes
from runtime.daemon.startup import scope_dir, unconfirmed_startup
from runtime.daemon.supervisor import _alive, kill_group, managed_popen
from tests.unit.runtime import test_quiescence
from tests.unit.runtime.test_quiescence import wait_file

rt = test_quiescence.rt


def suspended_interpreter(tmp_path):
    ready = tmp_path / "anchor-ready"
    interpreter = tmp_path / "interpreter"
    interpreter.write_text(
        "#!/bin/sh\n"
        f"printf '%s' $$ > {shlex.quote(str(ready))}.tmp\n"
        f"mv {shlex.quote(str(ready))}.tmp {shlex.quote(str(ready))}\n"
        "kill -STOP $$\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n'
    )
    interpreter.chmod(0o755)
    return interpreter, ready


def wait_suspended(ready):
    pid = int(wait_file(ready))
    deadline = time.monotonic() + 10
    while Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "T":
        assert time.monotonic() < deadline, "anchor never suspended"
        time.sleep(0.01)
    return pid


def driver_process(script, repo_root):
    return subprocess.Popen(
        [sys.executable, str(script)],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(repo_root), "HOME": str(script.parent)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@pytest.mark.parametrize("relaunch", ["ensure", "automatic"])
@pytest.mark.parametrize("kind", ["changes.capture", "snapshot.prepare"])
def test_service_relaunch_receipt_blocks_unnamed_anchor(
    rt, tmp_path, repo_root, monkeypatch, relaunch, kind
):
    interpreter, anchor_ready = suspended_interpreter(tmp_path)
    writer_ready, exit_first, release = (
        tmp_path / "writer-ready",
        tmp_path / "exit-first",
        tmp_path / "release",
    )
    changed = rt.daemon.worktree.path / "after-seal.txt"
    command = tmp_path / "service-command.py"
    command.write_text(
        "import os, time\nfrom pathlib import Path\n"
        f"first = Path({str(tmp_path / 'first-generation')!r})\n"
        "if not first.exists():\n"
        " first.touch()\n"
        f" while not Path({str(exit_first)!r}).exists(): time.sleep(0.01)\n"
        " raise SystemExit(1)\n"
        f"ready = Path({str(writer_ready)!r})\n"
        "ready.with_suffix('.tmp').write_text(str(os.getpid()))\n"
        "ready.with_suffix('.tmp').replace(ready)\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        f"Path({str(changed)!r}).write_text('unsafe late write')\n"
    )
    url_file = tmp_path / "url"
    driver = tmp_path / "service-daemon.py"
    driver.write_text(
        "import subprocess, time\nfrom pathlib import Path\n"
        "from tests.support.runtime import DaemonHarness\n"
        "original = subprocess.Popen\nlaunch_count = 0\n"
        "def launch(argv, **kwargs):\n"
        " global launch_count\n"
        " if len(argv) > 1 and str(argv[1]).endswith('/process_anchor.py'):\n"
        "  launch_count += 1\n"
        f"  if launch_count == 2: argv = [{str(interpreter)!r}, *argv[1:]]\n"
        " return original(argv, **kwargs)\n"
        "subprocess.Popen = launch\n"
        f"daemon = DaemonHarness(Path({str(rt.base)!r}))\n"
        "daemon.key = daemon.daemon.key = b'REDACTED'.ljust(32, b'_')\n"
        f"url = Path({str(url_file)!r})\n"
        "url.with_suffix('.tmp').write_text(daemon.url)\n"
        "url.with_suffix('.tmp').replace(url)\n"
        "while True: time.sleep(1)\n"
    )
    decl = {
        "name": "writer",
        "argv": [sys.executable, str(command)],
        "restart": "on-failure" if relaunch == "automatic" else "never",
    }
    rt.stop()
    rt.key = b"REDACTED".ljust(32, b"_")
    launcher = driver_process(driver, repo_root)
    anchor = writer_pid = request = None
    request_errors = []
    try:
        rt.url = wait_file(url_file)
        assert (
            rt.op("service.ensure", "first-ensure", {"declaration": decl}).json()["status"]
            == "succeeded"
        )
        if relaunch == "ensure":
            assert (
                rt.op("service.stop", "stop-first", {"name": "writer"}).json()["status"]
                == "succeeded"
            )

            def ensure_again():
                try:
                    rt.op("service.ensure", "second-ensure", {"declaration": decl})
                except httpx.TransportError as exc:
                    request_errors.append(exc)

            request = threading.Thread(target=ensure_again)
            request.start()
        else:
            exit_first.touch()
        anchor = wait_suspended(anchor_ready)
        launcher.kill()
        launcher.wait(timeout=10)
        if request is not None:
            request.join(timeout=10)
            assert not request.is_alive() and request_errors
        target, method = (changes, "capture") if kind == "changes.capture" else (None, "checkpoint")
        for restart in range(2):
            rt.start()
            assert "first-ensure" in rt.daemon.pending_writers
            assert unconfirmed_startup(scope_dir(rt.daemon.state_dir, "first-ensure"))
            assert _alive(anchor)
            with monkeypatch.context() as patch:
                patch.setattr(
                    target or rt.daemon.worktree,
                    method,
                    lambda *a, **kw: pytest.fail("sealed while an unnamed relaunch could write"),
                )
                result = rt.op(kind, f"blocked-{restart}", {"require_acked": False}).json()
                assert result["status"] == "failed" and result["result"]["error"]["code"] == "busy"
            if restart == 0:
                rt.stop()
        os.kill(anchor, signal.SIGCONT)
        writer_pid = int(wait_file(writer_ready))
        assert _alive(writer_pid)
        target = target or rt.daemon.worktree
        original = getattr(target, method)
        sealed = []

        def seal(*args, **kwargs):
            assert not _alive(writer_pid) and not _alive(anchor)
            sealed.append(True)
            release.touch()
            return original(*args, **kwargs)

        monkeypatch.setattr(target, method, seal)
        result = rt.op(kind, "safe-seal", {"base_sha": rt.base_sha, "require_acked": False}).json()
        assert result["status"] == "succeeded" and sealed, result
        assert not changed.exists() and not rt.daemon.pending_writers
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=10)
        if request is not None:
            request.join(timeout=10)
        kill_group(0, grace=0.1, scope="first-ensure")
        for pid in (writer_pid, anchor):
            if pid and _alive(pid):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize(
    "point", ["preexec", "interpreter", "never_spawned", "spawn_error", "before_tracking"]
)
def test_dead_startup_recovers_cancel_and_sealing(rt, tmp_path, repo_root, point):
    scope = "dead-startup"
    rt.daemon.journal.op_insert(scope, "turn.start", "digest", "sess_test", 1)
    startup_dir = (
        scope_dir(rt.daemon.state_dir, scope)
        if point == "before_tracking"
        else rt.daemon._track_writer(scope)
    )
    if point in ("preexec", "interpreter"):
        interpreter, ready = suspended_interpreter(tmp_path)
        driver = tmp_path / "dead-launch.py"
        setup = f"sys.executable = {str(interpreter)!r}\n"
        if point == "preexec":
            setup = (
                "original = subprocess.Popen\n"
                "def pause():\n"
                f" ready = Path({str(ready)!r})\n"
                " ready.with_suffix('.tmp').write_text(str(os.getpid()))\n"
                " ready.with_suffix('.tmp').replace(ready)\n"
                " os.kill(os.getpid(), signal.SIGSTOP)\n"
                "def spawn(*args, **kwargs):\n"
                " return original(*args, preexec_fn=pause, **kwargs)\n"
                "subprocess.Popen = spawn\n"
            )
        driver.write_text(
            "import os, signal, subprocess, sys\nfrom pathlib import Path\n"
            "from runtime.daemon.supervisor import managed_popen\n"
            + setup
            + f"managed_popen(['/bin/true'], scope={scope!r}, startup_dir=Path({str(startup_dir)!r}), "
            "env={'PATH': '/usr/bin:/bin'}, start_new_session=True)\n"
        )
        rt.daemon.journal.op_update(scope, status="starting")
        launcher = driver_process(driver, repo_root)
        anchor = None
        try:
            anchor = wait_suspended(ready)
            assert unconfirmed_startup(startup_dir)
            os.kill(anchor, signal.SIGKILL)
            launcher.wait(timeout=10)
            assert not _alive(anchor)
        finally:
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait(timeout=10)
            if anchor and _alive(anchor):
                os.kill(anchor, signal.SIGKILL)
    elif point == "spawn_error":
        with pytest.raises(FileNotFoundError):
            managed_popen(
                ["/bin/true"],
                scope=scope,
                startup_dir=startup_dir,
                env={"PATH": "/usr/bin:/bin"},
                cwd=str(tmp_path / "missing-directory"),
            )
    if startup_dir.exists():
        assert not unconfirmed_startup(startup_dir)
    for restart in range(3):
        rt.stop()
        rt.start()
        assert not rt.daemon.pending_writers and not rt.daemon.unconfirmed_launches
        cancelled = rt.op("turn.cancel", f"cancel-{restart}", {"target_operation_id": scope}).json()
        assert cancelled["result"]["stopped"] is True
        for kind in ("changes.capture", "snapshot.prepare"):
            result = rt.op(
                kind,
                f"{kind}-{restart}",
                {"base_sha": rt.base_sha, "require_acked": False},
            ).json()
            assert result["status"] == "succeeded", result
        result = rt.op(
            "files.write", f"write-{restart}", {"path": "safe.txt", "content": "safe"}
        ).json()
        assert result["status"] == "succeeded", result


@pytest.mark.parametrize("kind", ["changes.capture", "snapshot.prepare"])
def test_dead_anchor_after_spawn_keeps_startup_ambiguous(rt, tmp_path, repo_root, kind):
    scope = "lost-anchor"
    rt.daemon.journal.op_insert(scope, "turn.start", "digest", "sess_test", 1)
    startup_dir = rt.daemon._track_writer(scope)
    anchor_ready, writer_ready = tmp_path / "anchor-ready", tmp_path / "writer-ready"
    wrapper = tmp_path / "instrumented-anchor.py"
    wrapper.write_text(
        "import os, runpy, signal, subprocess, sys\nfrom pathlib import Path\n"
        "original = subprocess.Popen\n"
        "def launch(*args, **kwargs):\n"
        " child = original(*args, **kwargs)\n"
        f" writer = Path({str(writer_ready)!r})\n"
        " writer.with_suffix('.tmp').write_text(str(child.pid))\n"
        " writer.with_suffix('.tmp').replace(writer)\n"
        f" ready = Path({str(anchor_ready)!r})\n"
        " ready.with_suffix('.tmp').write_text(str(os.getpid()))\n"
        " ready.with_suffix('.tmp').replace(ready)\n"
        " os.kill(os.getpid(), signal.SIGSTOP)\n"
        " return child\n"
        "subprocess.Popen = launch\n"
        "sys.argv = sys.argv[1:]\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    interpreter = tmp_path / "instrumented-interpreter"
    interpreter.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(wrapper))} "$@"\n'
    )
    interpreter.chmod(0o755)
    driver = tmp_path / "launch.py"
    driver.write_text(
        "import subprocess, sys\nfrom pathlib import Path\n"
        "from runtime.daemon.supervisor import managed_popen\n"
        f"sys.executable = {str(interpreter)!r}\n"
        f"managed_popen(['/bin/sleep', '60'], scope={scope!r}, "
        f"startup_dir=Path({str(startup_dir)!r}), env={{'PATH': '/usr/bin:/bin'}}, "
        "start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    )
    launcher = driver_process(driver, repo_root)
    anchor = writer_pid = None
    try:
        anchor = wait_suspended(anchor_ready)
        writer_pid = int(wait_file(writer_ready))
        os.kill(anchor, signal.SIGKILL)
        launcher.wait(timeout=10)
        assert not _alive(anchor) and _alive(writer_pid)
        assert unconfirmed_startup(startup_dir), "spawn intent survives loss of kernel ancestry"
        for restart in range(2):
            rt.stop()
            rt.start()
            assert scope in rt.daemon.pending_writers
            result = rt.op(kind, f"blocked-{restart}", {"require_acked": False}).json()
            assert result["status"] == "failed" and result["result"]["error"]["code"] == "busy"
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=10)
        for pid in (writer_pid, anchor):
            if pid and _alive(pid):
                os.kill(pid, signal.SIGKILL)

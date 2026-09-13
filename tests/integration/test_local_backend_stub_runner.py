"""LocalProcessBackend drives stub_runner: streaming events and kill."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from control.backend import LocalProcessBackend, SandboxSpec


def _write_message(root: Path, text: str = "hello") -> Path:
    path = root / "msg.md"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def test_local_backend_runs_stub_runner_and_streams_events(stub_runner: Path) -> None:
    backend = LocalProcessBackend()
    handle = backend.create(SandboxSpec(tags={"case": "stream"}))
    init = backend.exec(
        handle,
        [sys.executable, str(stub_runner), "init", "--auth", "provider", "--model", "gpt-5"],
        env={"SBX_WORK": str(handle.root), "PYTHONUNBUFFERED": "1"},
    )
    assert init.wait() == 0
    config = (handle.root / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'approval_policy = "never"' in config
    assert 'sandbox_mode = "danger-full-access"' in config
    assert 'exclude = ["CODEX_AUTH_JSON"]' in config
    auth = handle.root / ".codex" / "auth.json"
    assert auth.stat().st_mode & 0o777 == 0o600

    msg = _write_message(handle.root)
    proc = backend.exec(
        handle,
        [
            sys.executable,
            str(stub_runner),
            "turn",
            "--n",
            "1",
            "--message-file",
            str(msg),
        ],
        env={
            "SBX_WORK": str(handle.root),
            "FAKE_CODEX_SCENARIO": "success",
            "PYTHONUNBUFFERED": "1",
        },
    )
    lines = list(proc.stdout)
    code = proc.wait()
    assert code == 0
    parsed = [json.loads(line) for line in lines if line.startswith("{")]
    types = [e["type"] for e in parsed]
    assert "sbx.turn_started" in types
    assert "thread.started" in types
    assert "sbx.turn_finished" in types
    finished = next(e for e in parsed if e["type"] == "sbx.turn_finished")
    assert finished["n"] == 1
    turn = json.loads((handle.root / "turns" / "1.json").read_text(encoding="utf-8"))
    assert set(turn) >= {"n", "codex_session_id", "status", "usage", "message", "exit_code"}
    assert (handle.root / "events.jsonl").is_file()
    assert (handle.root / "turns" / "1.json").is_file()
    assert (handle.root / "inbox" / "1.md").is_file()
    listed = backend.list(tags={"case": "stream"})
    assert any(h.id == handle.id for h in listed)
    poll = backend.poll(handle)
    assert poll.alive is True
    backend.terminate(handle)
    assert not handle.root.exists()


def test_local_backend_exec_stdin_is_devnull() -> None:
    backend = LocalProcessBackend()
    handle = backend.create(SandboxSpec(tags={"case": "stdin"}))
    proc = backend.exec(
        handle,
        [
            sys.executable,
            "-c",
            "import os; print(os.readlink('/proc/self/fd/0'), flush=True)",
        ],
        env={"PYTHONUNBUFFERED": "1"},
    )
    lines = [line.strip() for line in proc.stdout if line.strip()]
    assert proc.wait() == 0
    assert lines and lines[0] == "/dev/null"
    backend.terminate(handle)


def test_local_backend_kill_stops_hanging_stub_runner(stub_runner: Path) -> None:
    backend = LocalProcessBackend()
    handle = backend.create(SandboxSpec(tags={"case": "kill"}))
    backend.exec(
        handle,
        [sys.executable, str(stub_runner), "init", "--auth", "auth_json", "--model", "gpt-5"],
        env={"SBX_WORK": str(handle.root), "PYTHONUNBUFFERED": "1"},
    ).wait()
    msg = _write_message(handle.root)
    proc = backend.exec(
        handle,
        [
            sys.executable,
            str(stub_runner),
            "turn",
            "--n",
            "1",
            "--message-file",
            str(msg),
            "--max-seconds",
            "900",
        ],
        env={
            "SBX_WORK": str(handle.root),
            "FAKE_CODEX_SCENARIO": "hang",
            "PYTHONUNBUFFERED": "1",
        },
    )
    first = next(iter(proc.stdout))
    assert first
    started = time.monotonic()
    proc.kill()
    code = proc.wait()
    elapsed = time.monotonic() - started
    assert elapsed < 3.0
    poll = backend.poll(handle)
    assert poll.active_processes == 0
    backend.terminate(handle)
    assert code is not None


def test_overlapping_turn_exits_2_without_second_codex(stub_runner: Path) -> None:
    backend = LocalProcessBackend()
    handle = backend.create(SandboxSpec(tags={"case": "overlap"}))
    backend.exec(
        handle,
        [sys.executable, str(stub_runner), "init", "--auth", "provider", "--model", "gpt-5"],
        env={"SBX_WORK": str(handle.root), "PYTHONUNBUFFERED": "1"},
    ).wait()
    msg = _write_message(handle.root)
    hang = backend.exec(
        handle,
        [
            sys.executable,
            str(stub_runner),
            "turn",
            "--n",
            "1",
            "--message-file",
            str(msg),
            "--max-seconds",
            "900",
        ],
        env={
            "SBX_WORK": str(handle.root),
            "FAKE_CODEX_SCENARIO": "hang",
            "PYTHONUNBUFFERED": "1",
        },
    )
    assert next(iter(hang.stdout))
    second = backend.exec(
        handle,
        [
            sys.executable,
            str(stub_runner),
            "turn",
            "--n",
            "2",
            "--message-file",
            str(msg),
        ],
        env={
            "SBX_WORK": str(handle.root),
            "FAKE_CODEX_SCENARIO": "success",
            "PYTHONUNBUFFERED": "1",
        },
    )
    out = list(second.stdout)
    assert second.wait() == 2
    parsed = [json.loads(line) for line in out if line.startswith("{")]
    assert any(e.get("type") == "sbx.error" for e in parsed)
    assert not (handle.root / "inbox" / "2.md").exists()
    hang.kill()
    hang.wait()
    backend.terminate(handle)


def test_stop_with_no_running_turn_exits_0(stub_runner: Path) -> None:
    backend = LocalProcessBackend()
    handle = backend.create(SandboxSpec(tags={"case": "stop-idle"}))
    backend.exec(
        handle,
        [sys.executable, str(stub_runner), "init", "--auth", "provider", "--model", "gpt-5"],
        env={"SBX_WORK": str(handle.root), "PYTHONUNBUFFERED": "1"},
    ).wait()
    stop = backend.exec(
        handle,
        [sys.executable, str(stub_runner), "stop"],
        env={"SBX_WORK": str(handle.root), "PYTHONUNBUFFERED": "1"},
    )
    assert stop.wait() == 0
    backend.terminate(handle)


def test_concurrent_exec_and_killpg_reaps_children() -> None:
    backend = LocalProcessBackend()
    handle = backend.create(SandboxSpec(tags={"case": "pg"}))
    sleeper = (
        "import os, time, pathlib\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "pathlib.Path('child.pid').write_text(str(pid))\n"
        "time.sleep(30)\n"
    )
    child_proc = backend.exec(
        handle,
        [sys.executable, "-c", sleeper],
        env={"PYTHONUNBUFFERED": "1"},
    )
    peer = backend.exec(
        handle,
        [sys.executable, "-c", "import time; print('peer', flush=True); time.sleep(2)"],
        env={"PYTHONUNBUFFERED": "1"},
    )
    deadline = time.monotonic() + 3
    child_pid = None
    while time.monotonic() < deadline:
        path = handle.root / "child.pid"
        if path.is_file() and path.read_text(encoding="utf-8").strip().isdigit():
            child_pid = int(path.read_text(encoding="utf-8").strip())
            break
        time.sleep(0.05)
    assert child_pid is not None
    poll = backend.poll(handle)
    assert poll.active_processes == 2
    assert os_kill_exists(child_pid)
    child_proc.kill()
    child_proc.wait()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and os_kill_exists(child_pid):
        time.sleep(0.05)
    assert not os_kill_exists(child_pid)
    peer.wait()
    backend.terminate(handle)


def os_kill_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

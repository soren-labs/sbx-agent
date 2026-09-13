"""LocalProcessBackend drives stub_runner: streaming events and kill."""

from __future__ import annotations

import json
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
    assert (handle.root / "events.jsonl").is_file()
    assert (handle.root / "turns" / "1.json").is_file()
    assert (handle.root / "inbox" / "1.md").is_file()
    listed = backend.list(tags={"case": "stream"})
    assert any(h.id == handle.id for h in listed)
    poll = backend.poll(handle)
    assert poll.alive is True
    backend.terminate(handle)
    assert not handle.root.exists()


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

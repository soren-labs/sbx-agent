"""runner stop: SIGTERM current turn pid; no-op when idle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tests.unit.runner.conftest import init_runner, load_json, run_runner, write_message


def test_stop_with_no_running_turn_exits_0(runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    result = run_runner(["stop"], runner_env)
    assert result.returncode == 0


def test_stop_sigterm_hanging_turn(work: Path, runner_env: dict[str, str]) -> None:
    init_runner(runner_env)
    runner_env["FAKE_CODEX_SCENARIO"] = "hang"
    msg = write_message(work)
    turn_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "runtime.runner",
            "turn",
            "--n",
            "1",
            "--message-file",
            str(msg),
            "--max-seconds",
            "900",
        ],
        env=runner_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid: int | None = None
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        session_path = work / "session.json"
        if session_path.is_file():
            session = json.loads(session_path.read_text(encoding="utf-8"))
            if session.get("pid"):
                pid = int(session["pid"])
                break
        if turn_proc.poll() is not None:
            out, err = turn_proc.communicate()
            raise AssertionError(f"turn exited early rc={turn_proc.returncode} out={out} err={err}")
        time.sleep(0.05)
    assert pid is not None
    assert os.path.exists(f"/proc/{pid}")

    stop = run_runner(["stop"], runner_env)
    assert stop.returncode == 0
    turn_rc = turn_proc.wait(timeout=10)
    assert turn_rc in {0, 2, 3}
    assert not os.path.exists(f"/proc/{pid}")
    session = load_json(work / "session.json")
    assert session.get("pid") is None
    assert (work / "turns" / "1.json").is_file()

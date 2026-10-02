"""ACP bridge (runtime.runner.adapters.devin_acp) tests (SOR-72).

Drives the bridge as a subprocess against ``fake_acp.py``, a fake
``devin acp`` JSON-RPC stdio server. Covers success, resume (history-replay
suppression), nonzero, auth_invalid, badjson, slow, hang/SIGTERM, and child
env sanitisation.
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

FAKE_ACP = Path(__file__).resolve().parent / "fake_acp.py"
SESSION_ID = "fake-acp-session-01"


def _env(tmp_path: Path, repo_root: Path, scenario: str, **extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "SBX_WORK": str(tmp_path / "work"),
        "PYTHONPATH": str(repo_root),
        "PYTHONUNBUFFERED": "1",
        "DEVIN_BIN": str(FAKE_ACP),
        "FAKE_ACP_SCENARIO": scenario,
        # Intentionally poisoned: the bridge must strip these for `devin acp`.
        "ACP_BACKEND": "windsurf",
        "DEVIN_API_KEY": "sentinel",
        "DEVIN_V3_API_KEY": "sentinel-v3",
        "DEVIN_LEGACY_API_KEY": "sentinel-legacy",
        "DEVIN_ORG_ID": "sentinel-org",
        "WINDSURF_API_KEY": "sentinel-ws",
        "SBX_ACCOUNT_CREDENTIAL": "{}",
    }
    env.update(extra)
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    return env


def _run_bridge(
    env: dict[str, str],
    args: list[str],
    *,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "runtime.runner.adapters.devin_acp", *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _ndjson(stdout: str) -> list[dict]:
    out = []
    for line in stdout.splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def test_success_turn(tmp_path: Path, repo_root: Path) -> None:
    env = _env(tmp_path, repo_root, "success")
    proc = _run_bridge(env, ["--model", "swe-2-medium", "--", "do it"])
    assert proc.returncode == 0, proc.stderr
    events = _ndjson(proc.stdout)
    types = [e["type"] for e in events]
    assert types[0] == "session.started"
    assert events[0]["session_id"] == SESSION_ID
    assert events[0]["model"] == "swe-2-medium"
    assert events[0]["resumed"] is False
    assert types[1] == "turn.started"
    assert "reasoning" in types
    assert "tool_call" in types
    assert "tool_result" in types
    assert "assistant_message" in types
    assert types[-1] == "turn.completed"
    messages = [e for e in events if e["type"] == "assistant_message"]
    message = messages[-1]
    assert messages[0]["text"] == "Hello "
    assert messages[0]["status"] == "in_progress"
    assert len({e["id"] for e in messages}) == 1
    assert message["status"] == "completed"
    assert message["text"] == "Hello world"
    tool_call = next(e for e in events if e["type"] == "tool_call")
    assert tool_call["id"] == "exec:0"
    assert tool_call["input"]["command"] == "echo hi"
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["exit_code"] == 0
    usage = events[-1]["usage"]
    assert usage["input_tokens"] == 90
    assert usage["output_tokens"] == 10
    assert usage["cache_read_tokens"] == 50


def test_completed_tool_without_terminal_exit_defaults_zero(
    tmp_path: Path, repo_root: Path
) -> None:
    env = _env(tmp_path, repo_root, "no_terminal_exit")
    proc = _run_bridge(env, ["--model", "swe-2-medium", "--", "do it"])
    assert proc.returncode == 0, proc.stderr
    events = _ndjson(proc.stdout)
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["status"] == "completed"
    assert tool_result["exit_code"] == 0
    assert tool_result["output"] == "hi"


def test_resume_suppresses_history_replay(tmp_path: Path, repo_root: Path) -> None:
    env = _env(tmp_path, repo_root, "success")
    proc = _run_bridge(env, ["--resume", SESSION_ID, "--", "continue"])
    assert proc.returncode == 0, proc.stderr
    events = _ndjson(proc.stdout)
    assert events[0]["type"] == "session.started"
    assert events[0]["session_id"] == SESSION_ID
    assert events[0]["resumed"] is True
    messages = [e for e in events if e["type"] == "assistant_message"]
    assert all("REPLAYED" not in e["text"] for e in messages)
    assert not any("REPLAYED" in line for line in proc.stdout.splitlines())


def test_nonzero_prompt_error(tmp_path: Path, repo_root: Path) -> None:
    env = _env(tmp_path, repo_root, "nonzero")
    proc = _run_bridge(env, ["--model", "swe-2-medium", "--", "go"])
    assert proc.returncode == 1
    events = _ndjson(proc.stdout)
    assert any(e["type"] == "turn.failed" for e in events)
    assert "prompt exploded" in proc.stderr


def test_auth_invalid(tmp_path: Path, repo_root: Path) -> None:
    env = _env(tmp_path, repo_root, "auth_invalid")
    proc = _run_bridge(env, ["--model", "swe-2-medium", "--", "go"])
    assert proc.returncode == 1
    events = _ndjson(proc.stdout)
    assert events[-1]["type"] == "error"
    assert "401" in proc.stderr


def test_badjson_tolerated(tmp_path: Path, repo_root: Path) -> None:
    env = _env(tmp_path, repo_root, "badjson")
    proc = _run_bridge(env, ["--model", "swe-2-medium", "--", "go"])
    # The garbage line is skipped by the JSON-RPC reader; turn still completes.
    assert proc.returncode == 0, proc.stderr
    events = _ndjson(proc.stdout)
    assert events[-1]["type"] == "turn.completed"
    # It is not forwarded: forwarding would count as a bad line at the runner.
    assert "this is not json" not in proc.stdout


def test_slow_completes(tmp_path: Path, repo_root: Path) -> None:
    env = _env(tmp_path, repo_root, "slow", FAKE_ACP_SLOW_SECONDS="0.3")
    started = time.monotonic()
    proc = _run_bridge(env, ["--model", "swe-2-medium", "--", "go"])
    assert proc.returncode == 0
    assert time.monotonic() - started >= 0.3


def test_sigterm_kills_child_and_exits_nonzero(tmp_path: Path, repo_root: Path) -> None:
    pid_file = tmp_path / "acp.pid"
    env = _env(tmp_path, repo_root, "hang", FAKE_ACP_PID_FILE=str(pid_file))
    proc = subprocess.Popen(
        [sys.executable, "-m", "runtime.runner.adapters.devin_acp", "--", "go"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.is_file(), "fake acp child never started"
        child_pid = int(pid_file.read_text().strip())
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=15)
        assert rc != 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("devin acp child still alive after bridge SIGTERM")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_child_env_sanitised(tmp_path: Path, repo_root: Path) -> None:
    spy_out = tmp_path / "spy.json"
    env = _env(tmp_path, repo_root, "success", FAKE_ACP_SPY_OUT=str(spy_out))
    proc = _run_bridge(env, ["--model", "swe-2-medium", "--", "go"])
    assert proc.returncode == 0
    spy = json.loads(spy_out.read_text())
    for key in (
        "ACP_BACKEND",
        "DEVIN_API_KEY",
        "DEVIN_V3_API_KEY",
        "DEVIN_LEGACY_API_KEY",
        "DEVIN_ORG_ID",
        "WINDSURF_API_KEY",
        "SBX_ACCOUNT_CREDENTIAL",
    ):
        assert spy[key] is False, key
    assert spy["argv"][:2] == ["acp", "--model"]


def test_resume_argv_has_no_model(tmp_path: Path, repo_root: Path) -> None:
    spy_out = tmp_path / "spy.json"
    env = _env(tmp_path, repo_root, "success", FAKE_ACP_SPY_OUT=str(spy_out))
    proc = _run_bridge(env, ["--resume", SESSION_ID, "--", "go"])
    assert proc.returncode == 0
    spy = json.loads(spy_out.read_text())
    assert "--model" not in spy["argv"]
    assert spy["argv"] == ["acp"]


def test_workdir_follows_sbx_workdir(tmp_path: Path, repo_root: Path) -> None:
    """SOR-174: `devin acp` process cwd AND the ACP session ``cwd`` anchor
    at ``$SBX_WORK/$SBX_WORKDIR`` when the workspace workdir is declared."""
    workdir = tmp_path / "work" / "repo"
    workdir.mkdir(parents=True)
    spy_out = tmp_path / "spy.json"
    session_cwd_out = tmp_path / "session_cwd.txt"
    env = _env(
        tmp_path,
        repo_root,
        "success",
        SBX_WORKDIR="repo",
        FAKE_ACP_SPY_OUT=str(spy_out),
        FAKE_ACP_SESSION_CWD_OUT=str(session_cwd_out),
    )
    proc = _run_bridge(env, ["--model", "swe-2-medium", "--", "go"])
    assert proc.returncode == 0, proc.stderr
    spy = json.loads(spy_out.read_text())
    assert spy["cwd"] == str(workdir)
    assert session_cwd_out.read_text() == str(workdir)


def test_workdir_defaults_to_state_root(tmp_path: Path, repo_root: Path) -> None:
    """No SBX_WORKDIR: the pre-SOR-174 layout — `devin acp` cwd + ACP
    session ``cwd`` stay at ``$SBX_WORK``."""
    spy_out = tmp_path / "spy.json"
    session_cwd_out = tmp_path / "session_cwd.txt"
    env = _env(
        tmp_path,
        repo_root,
        "success",
        FAKE_ACP_SPY_OUT=str(spy_out),
        FAKE_ACP_SESSION_CWD_OUT=str(session_cwd_out),
    )
    proc = _run_bridge(env, ["--model", "swe-2-medium", "--", "go"])
    assert proc.returncode == 0, proc.stderr
    spy = json.loads(spy_out.read_text())
    assert spy["cwd"] == str(tmp_path / "work")
    assert session_cwd_out.read_text() == str(tmp_path / "work")

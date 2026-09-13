"""Unit tests for tests/fakes/fake_codex.py scenarios."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_THREAD = "01900000-0000-7000-8000-000000000001"


def _env(scenario: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["FAKE_CODEX_SCENARIO"] = scenario
    env["PYTHONUNBUFFERED"] = "1"
    if extra:
        env.update(extra)
    return env


def _run(
    fake_codex: Path,
    args: list[str],
    *,
    scenario: str,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
    timeout: float = 8.0,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(fake_codex), *args],
        cwd=cwd,
        env=_env(scenario, extra_env),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        input=stdin,
    )


def _events(stdout: str) -> list[dict]:
    out: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        out.append(json.loads(line))
    return out


def test_success_writes_hello_and_events(fake_codex: Path, tmp_path: Path) -> None:
    result = _run(
        fake_codex,
        ["exec", "--json", "--skip-git-repo-check", "-C", str(tmp_path), "hello"],
        scenario="success",
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hello from fake_codex\n"
    types = [e["type"] for e in _events(result.stdout)]
    assert types[0] == "thread.started"
    assert "turn.started" in types
    assert "item.started" in types
    assert "item.updated" in types
    assert "item.completed" in types
    assert types[-1] == "turn.completed"
    item_types = {
        e["item"]["type"]
        for e in _events(result.stdout)
        if e.get("type") == "item.completed" and isinstance(e.get("item"), dict)
    }
    assert item_types >= {"agent_message", "command_execution", "file_change"}
    usage = _events(result.stdout)[-1]["usage"]
    assert set(usage) >= {"input_tokens", "cached_input_tokens", "output_tokens"}


def test_resume_appends_hello_and_keeps_thread_id(fake_codex: Path, tmp_path: Path) -> None:
    first = _run(
        fake_codex,
        ["exec", "--json", "-C", str(tmp_path), "hello"],
        scenario="success",
        cwd=tmp_path,
    )
    assert first.returncode == 0
    thread_id = _events(first.stdout)[0]["thread_id"]
    second = _run(
        fake_codex,
        [
            "exec",
            "resume",
            "--json",
            "--skip-git-repo-check",
            "-C",
            str(tmp_path),
            thread_id,
            "again",
        ],
        scenario="resume",
        cwd=tmp_path,
    )
    assert second.returncode == 0, second.stderr
    text = (tmp_path / "hello.txt").read_text(encoding="utf-8")
    assert "hello from fake_codex" in text
    assert "resumed by fake_codex" in text
    resumed = _events(second.stdout)
    assert resumed[0]["type"] == "thread.started"
    assert resumed[0]["thread_id"] == thread_id == DEFAULT_THREAD


def test_resume_missing_hello_exits_1(fake_codex: Path, tmp_path: Path) -> None:
    result = _run(
        fake_codex,
        ["exec", "resume", "--json", DEFAULT_THREAD, "again"],
        scenario="resume",
        cwd=tmp_path,
    )
    assert result.returncode == 1
    ev = _events(result.stdout)
    assert any(e.get("type") == "error" for e in ev)
    assert not (tmp_path / "hello.txt").exists()


def test_nonzero_exits_1_with_stderr(fake_codex: Path, tmp_path: Path) -> None:
    result = _run(
        fake_codex,
        ["exec", "--json", "nope"],
        scenario="nonzero",
        cwd=tmp_path,
    )
    assert result.returncode == 1
    assert "nonzero" in result.stderr
    types = [e["type"] for e in _events(result.stdout)]
    assert "thread.started" in types
    assert "turn.completed" not in types


def test_hang_sigterm_exits_cleanly(fake_codex: Path, tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, str(fake_codex), "exec", "--json", "hang"],
        cwd=tmp_path,
        env=_env("hang"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert "thread.started" in line
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=3)
    assert proc.returncode == 0


def test_badjson_contains_non_json_line(fake_codex: Path, tmp_path: Path) -> None:
    result = _run(
        fake_codex,
        ["exec", "--json", "x"],
        scenario="badjson",
        cwd=tmp_path,
    )
    assert result.returncode == 0
    raw_lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    parsed_ok = 0
    bad = 0
    for line in raw_lines:
        try:
            json.loads(line)
            parsed_ok += 1
        except json.JSONDecodeError:
            bad += 1
    assert parsed_ok >= 2
    assert bad >= 1


def test_slow_honors_override_seconds(fake_codex: Path, tmp_path: Path) -> None:
    started = time.monotonic()
    result = _run(
        fake_codex,
        ["exec", "--json", "slow"],
        scenario="slow",
        cwd=tmp_path,
        extra_env={"FAKE_CODEX_SLOW_SECONDS": "1"},
        timeout=12.0,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert elapsed >= 1.0
    assert elapsed < 8.0
    types = [e["type"] for e in _events(result.stdout)]
    assert types[0] == "thread.started"
    assert types[-1] == "turn.completed"


def test_prompt_from_stdin_dash(fake_codex: Path, tmp_path: Path) -> None:
    result = _run(
        fake_codex,
        ["exec", "--json", "--dangerously-bypass-approvals-and-sandbox", "-"],
        scenario="success",
        cwd=tmp_path,
        stdin="prompt from stdin\n",
    )
    assert result.returncode == 0
    assert _events(result.stdout)[0]["type"] == "thread.started"


def test_resume_scenario_rejects_plain_exec(fake_codex: Path, tmp_path: Path) -> None:
    result = _run(
        fake_codex,
        ["exec", "--json", "nope"],
        scenario="resume",
        cwd=tmp_path,
    )
    assert result.returncode == 1
    assert any(e.get("type") == "error" for e in _events(result.stdout))

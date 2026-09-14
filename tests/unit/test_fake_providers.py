"""Scenario coverage for the non-Codex fake provider CLIs (SOR-59).

Each fake mirrors fake_codex semantics: cwd file writes, NDJSON stdout,
resume argv, and the seven shared scenarios (success, resume, nonzero,
hang, badjson, slow, auth_invalid).
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

FAKES = Path(__file__).resolve().parents[1] / "fakes"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "events"

# provider -> (script, first-turn argv, resume argv, scenario env, slow env)
FAKE_CLIS = {
    "antigravity": (
        "fake_agy.py",
        ["-p", "create hello.txt", "--output-format", "stream-json"],
        ["--resume", "conv-xyz", "-p", "update it"],
        "FAKE_AGY_SCENARIO",
        "FAKE_AGY_SLOW_SECONDS",
    ),
    "grok": (
        "fake_grok.py",
        ["-p", "create hello.txt", "--output-format", "streaming-json"],
        ["--session", "grok-xyz", "-p", "update it"],
        "FAKE_GROK_SCENARIO",
        "FAKE_GROK_SLOW_SECONDS",
    ),
    "opencode": (
        "fake_opencode.py",
        ["run", "create hello.txt"],
        ["run", "--session", "ses_xyz", "update it"],
        "FAKE_OPENCODE_SCENARIO",
        "FAKE_OPENCODE_SLOW_SECONDS",
    ),
    "devin": (
        "fake_devin.py",
        ["-p", "create hello.txt"],
        ["--resume", "devin-xyz", "-p", "update it"],
        "FAKE_DEVIN_SCENARIO",
        "FAKE_DEVIN_SLOW_SECONDS",
    ),
}

SCENARIOS = ("success", "resume", "nonzero", "hang", "badjson", "slow", "auth_invalid")


def _run(
    script: str,
    argv: list[str],
    cwd: Path,
    scenario_env: str,
    scenario: str,
    extra_env: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        scenario_env: scenario,
        "PYTHONUNBUFFERED": "1",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(FAKES / script), *argv],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.parametrize("provider", sorted(FAKE_CLIS))
def test_success_writes_hello_and_streams_json(provider: str, tmp_path: Path) -> None:
    script, argv, _resume, scenario_env, _slow = FAKE_CLIS[provider]
    proc = _run(script, argv, tmp_path, scenario_env, "success")
    assert proc.returncode == 0, proc.stderr
    hello = tmp_path / "hello.txt"
    assert hello.is_file()
    assert f"fake_{provider if provider != 'antigravity' else 'agy'}" in hello.read_text()
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, "expected NDJSON output"
    for ln in lines:
        json.loads(ln)


@pytest.mark.parametrize("provider", sorted(FAKE_CLIS))
def test_resume_appends_and_succeeds(provider: str, tmp_path: Path) -> None:
    script, argv, resume_argv, scenario_env, _slow = FAKE_CLIS[provider]
    first = _run(script, argv, tmp_path, scenario_env, "success")
    assert first.returncode == 0
    second = _run(script, resume_argv, tmp_path, scenario_env, "resume")
    assert second.returncode == 0, second.stderr
    hello = (tmp_path / "hello.txt").read_text(encoding="utf-8")
    assert hello.count("resumed by fake_") == 1


@pytest.mark.parametrize("provider", sorted(FAKE_CLIS))
def test_resume_scenario_requires_resume_argv(provider: str, tmp_path: Path) -> None:
    script, argv, _resume, scenario_env, _slow = FAKE_CLIS[provider]
    proc = _run(script, argv, tmp_path, scenario_env, "resume")
    assert proc.returncode == 1


@pytest.mark.parametrize("provider", sorted(FAKE_CLIS))
@pytest.mark.parametrize("scenario", ["nonzero", "auth_invalid"])
def test_failing_scenarios_exit_1(provider: str, scenario: str, tmp_path: Path) -> None:
    script, argv, _resume, scenario_env, _slow = FAKE_CLIS[provider]
    proc = _run(script, argv, tmp_path, scenario_env, scenario)
    assert proc.returncode == 1
    assert proc.stdout.strip() or proc.stderr.strip()


@pytest.mark.parametrize("provider", sorted(FAKE_CLIS))
def test_badjson_emits_malformed_line(provider: str, tmp_path: Path) -> None:
    script, argv, _resume, scenario_env, _slow = FAKE_CLIS[provider]
    proc = _run(script, argv, tmp_path, scenario_env, "badjson")
    assert proc.returncode == 0
    lines = proc.stdout.splitlines()
    assert "this is not json" in lines
    parsed = 0
    for ln in lines:
        try:
            json.loads(ln)
            parsed += 1
        except json.JSONDecodeError:
            pass
    assert parsed == len(lines) - 1


@pytest.mark.parametrize("provider", sorted(FAKE_CLIS))
def test_slow_pauses_then_completes(provider: str, tmp_path: Path) -> None:
    script, argv, _resume, scenario_env, slow_env = FAKE_CLIS[provider]
    started = time.monotonic()
    proc = _run(
        script,
        argv,
        tmp_path,
        scenario_env,
        "slow",
        extra_env={slow_env: "0.3"},
    )
    elapsed = time.monotonic() - started
    assert proc.returncode == 0
    assert elapsed >= 0.3


@pytest.mark.parametrize("provider", sorted(FAKE_CLIS))
def test_hang_emits_first_line_then_blocks(provider: str, tmp_path: Path) -> None:
    script, argv, _resume, scenario_env, _slow = FAKE_CLIS[provider]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        scenario_env: "hang",
        "PYTHONUNBUFFERED": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, str(FAKES / script), *argv],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        assert proc.stdout is not None
        first = proc.stdout.readline()
        assert first.strip(), "hang scenario must emit at least one line"
        # still running after the first line
        assert proc.poll() is None
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    assert proc.returncode == 0


@pytest.mark.parametrize("provider", sorted(FAKE_CLIS))
def test_fixtures_exist_and_parse(provider: str) -> None:
    fixture_dir = FIXTURES / provider
    for scenario in SCENARIOS:
        path = fixture_dir / f"{scenario}.jsonl"
        assert path.is_file(), f"missing {provider}/{scenario}.jsonl"
        bad = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                bad += 1
        # Only the badjson scenario may contain a malformed line.
        assert bad == (1 if scenario == "badjson" else 0), path

"""SOR-174: provider CLIs run inside the declared workspace workdir.

``$SBX_WORK`` stays the runner state root (session.json, events*.jsonl,
inbox/, turns/, home/) while ``SBX_WORKDIR`` (sandbox-root-relative, the
``WorkspaceRecord.workdir`` the control plane injects at turn dispatch)
pins the provider CLI's process cwd — plus codex ``-C``, opencode
``--dir``, and the ``devin acp`` process/ACP session ``cwd``. The WP0
fakes write hello.txt into their resolved cwd on success and append
``resumed by fake_*`` on resume, so file placement proves the cwd on both
first and resume turns.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from runtime.runner.workspace import agent_workdir
from tests.unit.runner.conftest import load_json, parsed_events, run_runner

FAKES = Path(__file__).resolve().parents[2] / "fakes"


def _env(work: Path, repo_root: Path, provider: str) -> dict[str, str]:
    bin_env = {
        "codex": ("CODEX_BIN", "fake_codex.py", "FAKE_CODEX_SCENARIO"),
        "antigravity": ("AGY_BIN", "fake_agy.py", "FAKE_AGY_SCENARIO"),
        "grok": ("GROK_BIN", "fake_grok.py", "FAKE_GROK_SCENARIO"),
        "opencode": ("OPENCODE_BIN", "fake_opencode.py", "FAKE_OPENCODE_SCENARIO"),
        "devin": ("DEVIN_BIN", "fake_devin.py", "FAKE_DEVIN_SCENARIO"),
    }[provider]
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "SBX_WORK": str(work),
        "PYTHONPATH": str(repo_root),
        "PYTHONUNBUFFERED": "1",
        "SBX_WORKDIR": "repo",
        bin_env[0]: str(FAKES / bin_env[1]),
        bin_env[2]: "success",
        "SBX_BACKEND": "local",
    }
    if provider == "devin":
        env["SBX_DEVIN_TRANSPORT"] = "cli"
    return env


def _init(env: dict[str, str], provider: str) -> None:
    args = ["init", "--provider", provider, "--model", "m-workdir"]
    if provider == "codex":
        args += ["--auth", "auth_json"]
    result = run_runner(args, env)
    assert result.returncode == 0, result.stderr


def _turn(env: dict[str, str], work: Path, n: int, *, timeout: float = 30.0) -> tuple[int, dict]:
    msg = work / f"msg{n}.md"
    msg.write_text(f"turn {n} prompt\n", encoding="utf-8")
    result = run_runner(
        ["turn", "--n", str(n), "--message-file", str(msg), "--max-seconds", "30"],
        env,
        timeout=timeout,
    )
    turn_path = work / "turns" / f"{n}.json"
    return result.returncode, load_json(turn_path) if turn_path.is_file() else {}


# Whether the provider's WP0 fake recognises the real resume argv (the
# agy fake only treats ``--resume`` as a resume marker, but the real CLI
# resumes via ``--conversation`` — its resume-turn cwd is proven with the
# success scenario instead).
_FAKE_SEES_RESUME = {
    "codex": True,
    "antigravity": False,
    "grok": True,
    "opencode": True,
    "devin": True,
}

# Hello/scenario markers use the fake's own short name, not the provider id.
_FAKE_NAME = {
    "codex": "fake_codex",
    "antigravity": "fake_agy",
    "grok": "fake_grok",
    "opencode": "fake_opencode",
    "devin": "fake_devin",
}


@pytest.mark.parametrize("provider", ["codex", "antigravity", "grok", "opencode", "devin"])
def test_cli_runs_in_declared_workdir_first_and_resume(
    work: Path, repo_root: Path, provider: str
) -> None:
    workdir = work / "repo"
    workdir.mkdir()
    env = _env(work, repo_root, provider)
    _init(env, provider)

    code, doc = _turn(env, work, 1)
    assert code == 0, doc
    hello = workdir / "hello.txt"
    assert hello.is_file()
    assert hello.read_text(encoding="utf-8") == f"hello from {_FAKE_NAME[provider]}\n"
    assert not (work / "hello.txt").exists()
    # Runner state stays under $SBX_WORK — nothing but the workdir output
    # lands inside repo/.
    assert (work / "session.json").is_file()
    assert (work / "events.jsonl").is_file()
    assert (work / "inbox" / "1.md").is_file()
    assert (work / "turns" / "1.json").is_file()
    assert not (workdir / "session.json").exists()
    assert doc["status"] == "success"

    scenario_env = {
        "codex": "FAKE_CODEX_SCENARIO",
        "antigravity": "FAKE_AGY_SCENARIO",
        "grok": "FAKE_GROK_SCENARIO",
        "opencode": "FAKE_OPENCODE_SCENARIO",
        "devin": "FAKE_DEVIN_SCENARIO",
    }[provider]
    if _FAKE_SEES_RESUME[provider]:
        env[scenario_env] = "resume"
    code2, doc2 = _turn(env, work, 2)
    assert code2 == 0, doc2
    if _FAKE_SEES_RESUME[provider]:
        assert f"resumed by {_FAKE_NAME[provider]}" in hello.read_text(encoding="utf-8")
    else:
        assert hello.read_text(encoding="utf-8") == f"hello from {_FAKE_NAME[provider]}\n"
    assert not (work / "hello.txt").exists()
    assert (work / "turns" / "2.json").is_file()


def test_agent_workdir_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.setenv("SBX_WORK", str(root))
    monkeypatch.delenv("SBX_WORKDIR", raising=False)
    assert agent_workdir() == root
    monkeypatch.setenv("SBX_WORKDIR", "repo")
    assert agent_workdir() == root / "repo"
    monkeypatch.setenv("SBX_WORKDIR", "nested/dir")
    assert agent_workdir() == root / "nested" / "dir"
    # Unsafe values fall back to the state root.
    for bad in ("", "/", "/abs", "..", "../x", "a/../b"):
        monkeypatch.setenv("SBX_WORKDIR", bad)
        assert agent_workdir() == root, bad


def test_no_workdir_keeps_state_root_cwd(work: Path, repo_root: Path) -> None:
    """Pre-SOR-174 layout: no SBX_WORKDIR -> CLI cwd is $SBX_WORK itself."""
    env = _env(work, repo_root, "codex")
    del env["SBX_WORKDIR"]
    _init(env, "codex")
    code, doc = _turn(env, work, 1)
    assert code == 0, doc
    assert (work / "hello.txt").is_file()
    assert not (work / "repo" / "hello.txt").exists()
    events = parsed_events(work)
    assert events[-1]["type"] == "sbx.turn_finished"
    assert events[-1]["status"] == "success"


def test_missing_workdir_dir_fails_loud(work: Path, repo_root: Path) -> None:
    """A declared but absent workdir must not silently run in $SBX_WORK."""
    env = _env(work, repo_root, "codex")
    _init(env, "codex")
    code, _doc = _turn(env, work, 1)
    assert code == 2
    assert not (work / "hello.txt").exists()
    events = parsed_events(work)
    errors = [e for e in events if e["type"] == "sbx.error"]
    assert errors and "failed to start provider CLI" in errors[0]["message"]

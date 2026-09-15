"""SOR-80: forward-compatible provider events end-to-end.

Regression for the confirmed blocker: a real Grok 1.0.24 run emitted
``{"type":"plan","entries":[{content,priority,status}]}`` mid-turn;
``GrokAdapter.translate`` returned ``[]`` and ``cmd_turn`` counted the
line as bad JSON, so the turn ended ``bad_json``/exit 4 despite CLI
success. Syntactically valid but unknown/non-terminal native events are
now acknowledged as ``sbx.noop`` (dropped before ``events.jsonl``);
malformed non-JSON lines remain fatal.

Each provider case replays a staged capture through the production argv
surface (``replay_grok.py`` / ``replay_agy.py`` / ``replay_devin.py``)
and asserts: exit 0, ``bad_json_lines == 0``, the unknown lines still
land in ``events.raw.jsonl``, and the turn completes normally.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from tests.unit.runner.conftest import load_json, parsed_events, run_runner

GROK_MODEL = "grok-4.6"
AGY_MODEL = "gemini-3.8-flash-low"
DEVIN_MODEL = "swe-2-high"

RUNNER_DIR = Path(__file__).resolve().parent
GROK_REPLAYER = RUNNER_DIR / "replay_grok.py"
AGY_REPLAYER = RUNNER_DIR / "replay_agy.py"
DEVIN_REPLAYER = RUNNER_DIR / "replay_devin.py"
GROK_FIXTURES = RUNNER_DIR / "fixtures" / "grok"
AGY_FIXTURES = RUNNER_DIR / "fixtures" / "antigravity"
DEVIN_FIXTURES = RUNNER_DIR / "fixtures" / "devin"

GROK_ID = "01a0a2d7-87bc-7192-b642-ad17e2cb3b55"
AGY_ID = "4e9eadd6-eb70-442b-b24a-1660db079181"
DEVIN_ID = "devin-session-01a09b11"


def _base_env(work: Path, repo_root: Path) -> dict[str, str]:
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "SBX_WORK": str(work),
        "PYTHONPATH": str(repo_root),
        "PYTHONUNBUFFERED": "1",
        "SBX_BACKEND": "local",
    }


@pytest.fixture
def grok_env(work: Path, repo_root: Path) -> dict[str, str]:
    env = _base_env(work, repo_root)
    env["GROK_BIN"] = str(GROK_REPLAYER)
    env["GROK_REPLAY_FIXTURE"] = str(GROK_FIXTURES / "plan.jsonl")
    return env


@pytest.fixture
def agy_env(work: Path, repo_root: Path) -> dict[str, str]:
    env = _base_env(work, repo_root)
    env["AGY_BIN"] = str(AGY_REPLAYER)
    env["AGY_REPLAY_FIXTURE"] = str(AGY_FIXTURES / "forward_compat.jsonl")
    return env


@pytest.fixture
def devin_env(work: Path, repo_root: Path) -> dict[str, str]:
    env = _base_env(work, repo_root)
    env["SBX_DEVIN_TRANSPORT"] = "cli"
    env["DEVIN_BIN"] = str(DEVIN_REPLAYER)
    env["DEVIN_REPLAY_FIXTURE"] = str(DEVIN_FIXTURES / "forward_compat.jsonl")
    return env


def _init(env: dict[str, str], provider: str, model: str) -> None:
    result = run_runner(["init", "--provider", provider, "--model", model], env)
    assert result.returncode == 0, result.stderr


def _turn(env: dict[str, str], work: Path, n: int = 1) -> tuple[int, dict, list[dict]]:
    msg = work / f"msg{n}.md"
    msg.write_text(f"turn {n} prompt\n", encoding="utf-8")
    result = run_runner(
        ["turn", "--n", str(n), "--message-file", str(msg), "--max-seconds", "30"],
        env,
        timeout=30.0,
    )
    turn_path = work / "turns" / f"{n}.json"
    doc = load_json(turn_path) if turn_path.is_file() else {}
    return result.returncode, doc, parsed_events(work)


def test_grok_plan_and_unknown_events_complete(work: Path, grok_env: dict[str, str]) -> None:
    """Real 1.0.24 plan blocks + a future unknown kind are tolerated; the
    turn still records thread.started and closes on end.sessionId/usage."""
    _init(grok_env, "grok", GROK_MODEL)
    code, doc, events = _turn(grok_env, work)
    assert code == 0
    assert doc["status"] == "success"
    assert doc["bad_json_lines"] == 0
    assert doc["native_session_id"] == GROK_ID
    types = [e["type"] for e in events]
    assert "thread.started" in types
    assert "turn.completed" in types
    assert types[-1] == "sbx.turn_finished"
    assert events[-1]["status"] == "success"
    assert not any(e["type"] == "sbx.error" for e in events)
    # Native lines are still preserved in the raw stream for debugging.
    raw = (work / "events.raw.jsonl").read_text(encoding="utf-8")
    assert '"type": "plan"' in raw
    assert '"type": "session_metrics"' in raw
    assert '"entries"' in raw


def test_agy_unknown_event_completes(work: Path, agy_env: dict[str, str]) -> None:
    _init(agy_env, "antigravity", AGY_MODEL)
    code, doc, events = _turn(agy_env, work)
    assert code == 0
    assert doc["status"] == "success"
    assert doc["bad_json_lines"] == 0
    assert doc["native_session_id"] == AGY_ID
    types = [e["type"] for e in events]
    assert "thread.started" in types
    assert "turn.completed" in types
    assert not any(e["type"] == "sbx.error" for e in events)
    raw = (work / "events.raw.jsonl").read_text(encoding="utf-8")
    assert '"event": "context_usage"' in raw


def test_devin_unknown_type_completes(work: Path, devin_env: dict[str, str]) -> None:
    """CLI transport: unknown ``type`` lines from ``devin -p`` are NOOP."""
    _init(devin_env, "devin", DEVIN_MODEL)
    code, doc, events = _turn(devin_env, work)
    assert code == 0
    assert doc["status"] == "success"
    assert doc["bad_json_lines"] == 0
    assert doc["native_session_id"] == DEVIN_ID
    types = [e["type"] for e in events]
    assert "thread.started" in types
    assert "turn.completed" in types
    assert not any(e["type"] == "sbx.error" for e in events)
    raw = (work / "events.raw.jsonl").read_text(encoding="utf-8")
    assert '"type": "workspace_notice"' in raw


def test_malformed_line_still_fatal(work: Path, grok_env: dict[str, str]) -> None:
    """Tolerating unknown events must not hide genuinely malformed lines:
    a non-JSON line still produces bad_json/exit 4 while the stream
    continues to the terminal completion."""
    fixture = work / "plan_badjson.jsonl"
    fixture.write_text(
        json.dumps(
            {
                "type": "plan",
                "entries": [
                    {"content": "Write marker.txt", "priority": "high", "status": "in_progress"}
                ],
            }
        )
        + "\nthis is not json\n"
        + json.dumps(
            {
                "type": "end",
                "stopReason": "end_turn",
                "sessionId": GROK_ID,
                "usage": {"input_tokens": 17255, "output_tokens": 142},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    grok_env["GROK_REPLAY_FIXTURE"] = str(fixture)
    _init(grok_env, "grok", GROK_MODEL)
    code, doc, events = _turn(grok_env, work)
    assert code == 4
    assert doc["status"] == "bad_json"
    assert doc["bad_json_lines"] >= 1
    assert any(e["type"] == "sbx.error" for e in events)
    assert any(e["type"] == "turn.completed" for e in events)

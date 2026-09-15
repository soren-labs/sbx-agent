"""GrokAdapter unit tests: argv, prepare_home, translate, health (SOR-62).

Translate coverage uses both the staged real SOR-60 captures
(``tests/unit/runner/fixtures/grok/*.jsonl`` — real ``streaming-json``:
``available_commands``/``thought{data}``/``text{data}``/step-level
``usage``/``tool_call``/``tool_call_update``/``end``/flat ``error``; the
session id only appears on the terminal ``end.sessionId``) and the WP0
hand-written fixtures (``tests/fixtures/events/grok/`` —
``init{sessionId}``, ``thought{text}``/``text{text}``,
``tool_call{name,arguments}``/``tool_result{name,exit_code,output}``,
``end.usage`` WP0 field names, nested ``error{error:{message}}``).
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from runtime.runner.adapter import get_adapter
from runtime.runner.adapters.grok import GROK_AUTH_REL, GrokAdapter
from runtime.runner.constants import NOOP_EVENT_TYPE

ROOT = Path(__file__).resolve().parents[3]
REAL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "grok"
WP0_FIXTURES = ROOT / "tests" / "fixtures" / "events" / "grok"

MODEL = "grok-4.6"
REAL_ID = "01a0a2d7-87bc-7192-b642-ad17e2cb3b55"
WP0_ID = "01a09b11-0000-7f90-b96e-42adeefa05e0"
WRITE_CALL = "call-ef59fcee-b1fe-4f4e-9548-37899cfc8a6f-0"
PWD_CALL = "call-ef59fcee-b1fe-4f4e-9548-37899cfc8a6f-1"


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _translate_all(lines: list[str], adapter: GrokAdapter | None = None) -> list[dict]:
    adapter = adapter or GrokAdapter()
    out: list[dict] = []
    for line in lines:
        out.extend(adapter.translate(line))
    return [e for e in out if e.get("type") != NOOP_EVENT_TYPE]


def test_registered() -> None:
    adapter = get_adapter("grok")
    assert isinstance(adapter, GrokAdapter)
    assert adapter.provider == "grok"


def test_credential_files_single_auth_json() -> None:
    assert GrokAdapter().credential_files == (".grok/auth.json",)


def test_first_turn_argv_matches_spike(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROK_BIN", raising=False)
    argv = GrokAdapter().first_turn_argv("do the thing", MODEL)
    assert argv == [
        "grok",
        "-p",
        "do the thing",
        "--output-format",
        "streaming-json",
        "--model",
        MODEL,
        "--permission-mode",
        "bypassPermissions",
    ]


def test_first_turn_argv_omits_empty_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROK_BIN", raising=False)
    argv = GrokAdapter().first_turn_argv("hi", "")
    assert "--model" not in argv


def test_grok_bin_py_gets_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROK_BIN", "/opt/fakes/fake_grok.py")
    argv = GrokAdapter().first_turn_argv("hi", MODEL)
    assert argv[:2] == [sys.executable, "/opt/fakes/fake_grok.py"]


def _write_session(work: Path, **fields: object) -> None:
    work.mkdir(parents=True, exist_ok=True)
    (work / "session.json").write_text(json.dumps(fields), encoding="utf-8")


def test_resume_argv_matches_spike(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROK_BIN", raising=False)
    monkeypatch.setenv("SBX_WORK", str(tmp_path))
    _write_session(tmp_path, model=MODEL)
    argv = GrokAdapter().resume_argv("next please", "sess-xyz")
    assert argv == [
        "grok",
        "-p",
        "next please",
        "--resume",
        "sess-xyz",
        "--output-format",
        "streaming-json",
        "--model",
        MODEL,
        "--permission-mode",
        "bypassPermissions",
    ]


def test_resume_argv_without_session_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROK_BIN", raising=False)
    monkeypatch.setenv("SBX_WORK", str(tmp_path))
    argv = GrokAdapter().resume_argv("next", "sess-xyz")
    assert argv[argv.index("--resume") + 1] == "sess-xyz"
    assert "--model" not in argv


def test_prepare_home_safe_permissions(tmp_path: Path) -> None:
    auth = tmp_path / GROK_AUTH_REL
    auth.parent.mkdir(parents=True)
    auth.write_text(
        '{"https://auth.x.ai::0000":{"key":"REDACTED","refresh_token":"REDACTED"}}\n',
        encoding="utf-8",
    )
    auth.chmod(0o644)
    GrokAdapter().prepare_home(tmp_path, MODEL)
    assert stat.S_IMODE((tmp_path / ".grok").stat().st_mode) == 0o700
    # Content is never rewritten; only the mode is tightened.
    assert "REDACTED" in auth.read_text(encoding="utf-8")
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600


def test_prepare_home_without_credential(tmp_path: Path) -> None:
    GrokAdapter().prepare_home(tmp_path, MODEL)
    grok_dir = tmp_path / ".grok"
    assert grok_dir.is_dir()
    assert stat.S_IMODE(grok_dir.stat().st_mode) == 0o700
    assert not (grok_dir / "auth.json").exists()


def test_translate_real_success_fixture() -> None:
    events = _translate_all(_lines(REAL_FIXTURES / "success.jsonl"))
    types = [e["type"] for e in events]
    assert types[0] == "turn.started"
    # streaming-json: the session id only arrives on the terminal `end`.
    assert types[-1] == "turn.completed"
    assert types[-3] == "thread.started"
    assert events[-3]["thread_id"] == REAL_ID

    items = [e["item"] for e in events if isinstance(e.get("item"), dict)]
    kinds = {i["type"] for i in items}
    assert {"reasoning", "command_execution", "file_change", "agent_message"} <= kinds

    started = {
        e["item"]["id"]: e["item"]
        for e in events
        if e["type"] == "item.started" and isinstance(e.get("item"), dict)
    }
    completed = {
        e["item"]["id"]: e["item"]
        for e in events
        if e["type"] == "item.completed" and isinstance(e.get("item"), dict)
    }
    # toolCallId is the item id; started/completed share it.
    assert started[PWD_CALL]["type"] == "command_execution"
    assert started[PWD_CALL]["command"] == "pwd"
    assert started[PWD_CALL]["exit_code"] is None
    assert completed[PWD_CALL]["command"] == "pwd"
    assert completed[PWD_CALL]["aggregated_output"] == "exit: 0\n/work\n"
    assert completed[PWD_CALL]["exit_code"] == 0
    assert completed[PWD_CALL]["status"] == "completed"

    assert started[WRITE_CALL]["type"] == "file_change"
    assert completed[WRITE_CALL]["changes"][0]["path"] == "/work/marker.txt"
    assert completed[WRITE_CALL]["status"] == "completed"

    messages = [i for i in items if i["type"] == "agent_message"]
    # text.data deltas across each segment concatenate into whole items.
    assert [m["text"] for m in messages] == [
        "I'll create `marker.txt` with the exact contents you specified, then run `pwd`.",
        "DONE",
    ]
    reasoning = [i for i in items if i["type"] == "reasoning"]
    assert len(reasoning) == 2
    assert reasoning[0]["text"].startswith("The user wants me to")

    # Canonical usage comes from the terminal end.usage, not step-level.
    usage = events[-1]["usage"]
    assert usage["input_tokens"] == 17255
    assert usage["cached_input_tokens"] == 16768
    assert usage["cache_write_input_tokens"] == 0
    assert usage["output_tokens"] == 142
    assert usage["reasoning_output_tokens"] == 66


def test_translate_real_success_tolerates_null_status_update() -> None:
    """The real CLI emits a first tool_call_update with status:null — it must
    not be treated as a terminal update."""
    adapter = GrokAdapter()
    events = _translate_all(_lines(REAL_FIXTURES / "success.jsonl"), adapter)
    # Exactly one item.completed per tool call (the null/in_progress
    # updates produce no terminal item).
    completed = [e for e in events if e["type"] == "item.completed" and e["item"]["id"] == PWD_CALL]
    assert len(completed) == 1
    assert completed[0]["item"]["status"] == "completed"


def test_translate_real_resume_fixture() -> None:
    events = _translate_all(_lines(REAL_FIXTURES / "resume.jsonl"))
    started = [e for e in events if e["type"] == "thread.started"]
    assert started == [{"type": "thread.started", "thread_id": REAL_ID}]
    messages = [
        e["item"]
        for e in events
        if e["type"] == "item.completed"
        and isinstance(e.get("item"), dict)
        and e["item"]["type"] == "agent_message"
    ]
    assert [m["text"] for m in messages] == ["FILE=marker.txt CONTENT=spike-marker-001"]
    assert events[-1]["type"] == "turn.completed"
    assert events[-1]["usage"]["cached_input_tokens"] == 17024


def test_translate_real_nonzero_fixture() -> None:
    events = _translate_all(_lines(REAL_FIXTURES / "nonzero.jsonl"))
    types = [e["type"] for e in events]
    assert "error" in types
    assert types[-1] == "turn.failed"
    assert events[-1]["error"]["message"].startswith("Couldn't set model")


def test_translate_real_auth_invalid_fixture() -> None:
    events = _translate_all(_lines(REAL_FIXTURES / "auth_invalid.jsonl"))
    assert events[-1]["type"] == "turn.failed"
    assert events[-1]["error"]["message"].startswith("Not signed in.")
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "grok login --device-code" in errors[0]["message"]


def test_real_fixture_lines_all_translate() -> None:
    """Every real capture line yields >=1 event (NOOP counts)."""
    for fixture in REAL_FIXTURES.glob("*.jsonl"):
        adapter = GrokAdapter()
        for line in _lines(fixture):
            if not line.strip():
                continue
            events = adapter.translate(line)
            assert events, f"{fixture.name}: {line[:80]}"


def test_translate_wp0_success_fixture() -> None:
    """WP0 fake shape: init line, text fields, tool_call/tool_result pair."""
    events = _translate_all(_lines(WP0_FIXTURES / "success.jsonl"))
    assert events[0] == {"type": "thread.started", "thread_id": WP0_ID}
    types = [e["type"] for e in events]
    assert types[1] == "turn.started"
    items = [e["item"] for e in events if isinstance(e.get("item"), dict)]
    kinds = {i["type"] for i in items}
    assert {"reasoning", "command_execution", "agent_message"} <= kinds
    started = [i for i in items if i["type"] == "command_execution"]
    assert started[0]["command"] == "printf 'hello from fake_grok\\n' > hello.txt"
    assert events[-1]["type"] == "turn.completed"
    usage = events[-1]["usage"]
    assert usage["input_tokens"] == 8100
    assert usage["output_tokens"] == 210
    assert usage["reasoning_output_tokens"] == 120
    assert usage["cached_input_tokens"] == 5400
    assert usage["cache_write_input_tokens"] == 0


def test_translate_wp0_error_object() -> None:
    events = _translate_all(_lines(WP0_FIXTURES / "auth_invalid.jsonl"))
    assert events[-1]["type"] == "turn.failed"
    assert events[-1]["error"]["message"] == "Invalid or expired API key"


def test_wp0_fixture_lines_all_translate() -> None:
    """Every WP0 fixture line yields >=1 event except the intentional bad line."""
    for fixture in WP0_FIXTURES.glob("*.jsonl"):
        adapter = GrokAdapter()
        for line in _lines(fixture):
            if not line.strip():
                continue
            events = adapter.translate(line)
            if "not json" in line:
                assert events == []
            else:
                assert events, f"{fixture.name}: {line[:80]}"


def test_translate_stale_resume_mismatch() -> None:
    """end.sessionId != requested --resume id -> error, no thread.started."""
    adapter = GrokAdapter()
    adapter.resume_argv("next", "requested-id")
    events = _translate_all(
        [
            json.dumps(
                {
                    "type": "end",
                    "stopReason": "end_turn",
                    "sessionId": "new-id",
                    "usage": {},
                }
            )
        ],
        adapter,
    )
    types = [e["type"] for e in events]
    assert "thread.started" not in types
    assert "error" in types
    assert "requested-id" in events[1]["message"]
    assert "new-id" in events[1]["message"]
    assert types[-1] == "turn.failed"
    assert adapter.extract_session_id(events) is None


def test_translate_matching_resume_id() -> None:
    adapter = GrokAdapter()
    adapter.resume_argv("next", REAL_ID)
    events = _translate_all(_lines(REAL_FIXTURES / "resume.jsonl"), adapter)
    started = [e for e in events if e["type"] == "thread.started"]
    assert started == [{"type": "thread.started", "thread_id": REAL_ID}]
    assert events[-1]["type"] == "turn.completed"


def test_translate_messages_json_defensive() -> None:
    """streaming-messages-json lines (spike-recommended fallback format):
    system.init -> thread.started, assistant blocks -> items, result ->
    turn.completed."""
    events = _translate_all(
        [
            json.dumps({"type": "system.init", "session_id": "msg-sess-1", "model": MODEL}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "hmm"},
                            {"type": "text", "text": "PONG"},
                        ],
                    },
                    "session_id": "msg-sess-1",
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": "msg-sess-1",
                    "result": "PONG",
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                }
            ),
        ]
    )
    types = [e["type"] for e in events]
    assert types[0] == "thread.started"
    assert events[0]["thread_id"] == "msg-sess-1"
    assert "turn.started" in types
    items = [e["item"] for e in events if isinstance(e.get("item"), dict)]
    assert {i["type"] for i in items} == {"reasoning", "agent_message"}
    assert types[-1] == "turn.completed"
    assert events[-1]["usage"]["input_tokens"] == 5
    assert events[-1]["usage"]["output_tokens"] == 3


def test_translate_non_object_lines_return_empty() -> None:
    """Only lines with no JSON object return [] (runner counts them bad)."""
    adapter = GrokAdapter()
    assert adapter.translate("this is not json") == []
    assert adapter.translate("") == []
    assert adapter.translate("[1,2,3]") == []


def test_translate_unknown_and_plan_kinds_are_noop() -> None:
    """SOR-80: parseable objects never return []; unknown kinds and the
    real 1.0.24 ``plan`` progress block are acknowledged as NOOP."""
    adapter = GrokAdapter()
    assert adapter.translate('{"type": "mystery"}') == [{"type": NOOP_EVENT_TYPE}]
    plan = json.dumps(
        {
            "type": "plan",
            "entries": [
                {"content": "Inspect the workspace", "priority": "high", "status": "completed"},
                {"content": "Write marker.txt", "priority": "high", "status": "in_progress"},
                {"content": "Verify output", "priority": "medium", "status": "pending"},
            ],
        }
    )
    assert adapter.translate(plan) == [{"type": NOOP_EVENT_TYPE}]
    assert adapter.translate('{"type": "tool_call_update", "toolCallId": "x"}') == [
        {"type": NOOP_EVENT_TYPE}
    ]


def test_translate_plan_then_completion() -> None:
    """Real 1.0.24 shape: plan entries, then end.sessionId + usage close
    the turn — the plan event must not poison the stream."""
    events = _translate_all(_lines(REAL_FIXTURES / "plan.jsonl"))
    types = [e["type"] for e in events]
    assert types[0] == "turn.started"
    assert "thread.started" in types
    started = [e for e in events if e["type"] == "thread.started"]
    assert started[0]["thread_id"] == REAL_ID
    assert types[-1] == "turn.completed"
    assert events[-1]["usage"]["input_tokens"] == 17255
    messages = [
        e["item"]
        for e in events
        if e["type"] == "item.completed" and e["item"]["type"] == "agent_message"
    ]
    assert messages[-1]["text"] == "DONE"


def test_extract_session_id() -> None:
    adapter = GrokAdapter()
    events = _translate_all(_lines(REAL_FIXTURES / "success.jsonl"), adapter)
    assert adapter.extract_session_id(events) == REAL_ID
    assert adapter.extract_session_id([{"type": "turn.started"}]) is None


def test_health_from() -> None:
    adapter = GrokAdapter()
    assert adapter.health_from(0, "") == "ok"
    assert (
        adapter.health_from(
            1,
            "Error: Not signed in. To authenticate without a browser, run:\n"
            "  grok login --device-code",
        )
        == "auth_invalid"
    )
    assert adapter.health_from(1, "grok auth_invalid scenario") == "auth_invalid"
    assert adapter.health_from(1, "HTTP 429 too many requests") == "rate_limited"
    assert adapter.health_from(1, "RESOURCE_EXHAUSTED: quota") == "rate_limited"
    # Stale --resume id: 404 remote-restore failure is a session error, not
    # an account-auth failure -> unknown (runner exits 2).
    assert (
        adapter.health_from(
            1,
            'Session "x" not found locally, restoring conversation from remote...\n'
            "Error: Failed to restore session from remote: HttpError(404 Not Found)",
        )
        == "unknown"
    )
    assert adapter.health_from(1, "something else exploded") == "unknown"
    assert adapter.health_from(None, "") == "unknown"

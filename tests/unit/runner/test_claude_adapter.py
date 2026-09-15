"""ClaudeAdapter unit tests: argv, prepare_home, translate, health (SOR-97).

The provider stays **Experimental**: it is not in the frozen
``adapter.py`` registry, so ``get_adapter("claude")`` must keep raising
KeyError. Translate coverage uses staged captures under
``tests/unit/runner/fixtures/claude/`` — ``auth_invalid`` and
``stale_session`` mirror real 2.1.250 CLI output (fresh-HOME no-auth
probe and bogus-``--resume`` probe); ``success``/``resume``/
``rate_limited``/``nonzero`` follow the same verified line shapes with
Messages-API content blocks (``thinking``/``text``/``tool_use``/
``tool_result``) confirmed against local session transcripts.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from runtime.runner.adapter import AgentAdapter, get_adapter
from runtime.runner.adapters.claude import CLAUDE_CREDENTIALS_REL, ClaudeAdapter
from runtime.runner.constants import NOOP_EVENT_TYPE

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude"

MODEL = "claude-sonnet-4.6"
SESS_ID = "f00dcafe-1234-4abc-8def-0000000000c1"
AUTH_ID = "6b382e55-a657-4719-9122-16cc955c205f"


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _translate_all(lines: list[str], adapter: ClaudeAdapter | None = None) -> list[dict]:
    adapter = adapter or ClaudeAdapter()
    out: list[dict] = []
    for line in lines:
        out.extend(adapter.translate(line))
    return [e for e in out if e.get("type") != NOOP_EVENT_TYPE]


def test_not_registered_experimental() -> None:
    """claude is not in the frozen provider set: registry lookup fails."""
    with pytest.raises(KeyError):
        get_adapter("claude")


def test_protocol_conformance() -> None:
    adapter = ClaudeAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.provider == "claude"


def test_credential_files_single_credentials_json() -> None:
    assert ClaudeAdapter().credential_files == (".claude/.credentials.json",)


def test_first_turn_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setenv("SBX_WORK", str(tmp_path))
    argv = ClaudeAdapter().first_turn_argv("do the thing", MODEL)
    assert argv == [
        "claude",
        "-p",
        "do the thing",
        "--output-format",
        "stream-json",
        "--model",
        MODEL,
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
    ]


def test_first_turn_argv_omits_empty_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setenv("SBX_WORK", str(tmp_path))
    argv = ClaudeAdapter().first_turn_argv("hi", "")
    assert "--model" not in argv
    assert argv[argv.index("-p") + 1] == "hi"


def test_claude_bin_py_gets_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_BIN", "/opt/fakes/replay_claude.py")
    argv = ClaudeAdapter().first_turn_argv("hi", MODEL)
    assert argv[:2] == [sys.executable, "/opt/fakes/replay_claude.py"]


def _write_session(work: Path, **fields: object) -> None:
    work.mkdir(parents=True, exist_ok=True)
    (work / "session.json").write_text(json.dumps(fields), encoding="utf-8")


def test_resume_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setenv("SBX_WORK", str(tmp_path))
    _write_session(tmp_path, model=MODEL)
    argv = ClaudeAdapter().resume_argv("next please", SESS_ID)
    assert argv == [
        "claude",
        "-p",
        "next please",
        "--resume",
        SESS_ID,
        "--output-format",
        "stream-json",
        "--model",
        MODEL,
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
    ]


def test_resume_argv_without_session_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setenv("SBX_WORK", str(tmp_path))
    argv = ClaudeAdapter().resume_argv("next", SESS_ID)
    assert argv[argv.index("--resume") + 1] == SESS_ID
    assert "--model" not in argv


def test_prepare_home_safe_permissions(tmp_path: Path) -> None:
    cred = tmp_path / CLAUDE_CREDENTIALS_REL
    cred.parent.mkdir(parents=True)
    cred.write_text(
        '{"claudeAiOauth":{"accessToken":"REDACTED","refreshToken":"REDACTED",'
        '"expiresAt":2000000000000,"subscriptionType":"claude_pro"}}\n',
        encoding="utf-8",
    )
    cred.chmod(0o644)
    ClaudeAdapter().prepare_home(tmp_path, MODEL)
    claude_dir = tmp_path / ".claude"
    assert stat.S_IMODE(claude_dir.stat().st_mode) == 0o700
    # Content is never rewritten; only the mode is tightened.
    assert "REDACTED" in cred.read_text(encoding="utf-8")
    assert stat.S_IMODE(cred.stat().st_mode) == 0o600


def test_prepare_home_without_credential(tmp_path: Path) -> None:
    ClaudeAdapter().prepare_home(tmp_path, MODEL)
    claude_dir = tmp_path / ".claude"
    assert claude_dir.is_dir()
    assert stat.S_IMODE(claude_dir.stat().st_mode) == 0o700
    assert not (claude_dir / ".credentials.json").exists()


def test_translate_success_fixture() -> None:
    events = _translate_all(_lines(FIXTURES / "success.jsonl"))
    types = [e["type"] for e in events]
    assert types[0] == "thread.started"
    assert events[0]["thread_id"] == SESS_ID
    assert types[1] == "turn.started"
    assert types[-1] == "turn.completed"

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
    # tool_use block id is the item id; tool_result pairs by tool_use_id.
    assert started["toolu_0100"]["type"] == "command_execution"
    assert started["toolu_0100"]["command"] == "printf 'spike-marker-001\\n' > marker.txt"
    assert completed["toolu_0100"]["exit_code"] == 0
    assert completed["toolu_0100"]["status"] == "completed"
    assert started["toolu_0200"]["type"] == "file_change"
    assert completed["toolu_0200"]["changes"][0]["path"] == "/work/marker.txt"

    reasoning = [i for i in items if i["type"] == "reasoning"]
    assert [r["text"] for r in reasoning] == ["I will write marker.txt and verify it."]
    messages = [i for i in items if i["type"] == "agent_message"]
    assert [m["text"] for m in messages] == ["Created marker.txt in the workspace."]

    # Real 2.1.250 usage field names -> canonical five fields.
    usage = events[-1]["usage"]
    assert usage["input_tokens"] == 11200
    assert usage["output_tokens"] == 42
    assert usage["reasoning_output_tokens"] == 10
    assert usage["cached_input_tokens"] == 2000
    assert usage["cache_write_input_tokens"] == 500


def test_translate_resume_fixture() -> None:
    events = _translate_all(_lines(FIXTURES / "resume.jsonl"))
    started = [e for e in events if e["type"] == "thread.started"]
    assert started == [{"type": "thread.started", "thread_id": SESS_ID}]
    messages = [
        e["item"]
        for e in events
        if e["type"] == "item.completed"
        and isinstance(e.get("item"), dict)
        and e["item"]["type"] == "agent_message"
    ]
    assert [m["text"] for m in messages] == ["FILE=marker.txt CONTENT=spike-marker-001"]
    assert events[-1]["type"] == "turn.completed"
    assert events[-1]["usage"]["cached_input_tokens"] == 4000


def test_translate_auth_invalid_fixture() -> None:
    """Real fresh-HOME capture: rc=0 but the stream carries the auth
    failure — error + turn.failed, and the health flag is recorded."""
    adapter = ClaudeAdapter()
    events = _translate_all(_lines(FIXTURES / "auth_invalid.jsonl"), adapter)
    types = [e["type"] for e in events]
    assert types[0] == "thread.started"  # init still allocates a session id
    assert "error" in types
    assert types[-1] == "turn.failed"
    errors = [e for e in events if e["type"] == "error"]
    assert "Not logged in" in errors[0]["message"]
    assert events[-1]["error"]["message"] == "Not logged in · Please run /login"
    # rc=0 is masked by the CLI; the recorded flag still classifies it.
    assert adapter.health_from(0, "") == "auth_invalid"


def test_translate_rate_limited_fixture() -> None:
    adapter = ClaudeAdapter()
    events = _translate_all(_lines(FIXTURES / "rate_limited.jsonl"), adapter)
    assert events[-1]["type"] == "turn.failed"
    assert "429" in events[-1]["error"]["message"]
    assert adapter.health_from(0, "") == "rate_limited"


def test_translate_nonzero_fixture() -> None:
    events = _translate_all(_lines(FIXTURES / "nonzero.jsonl"))
    types = [e["type"] for e in events]
    assert "error" in types
    assert types[-1] == "turn.failed"
    message = events[-1]["error"]["message"]
    assert message == "API Error: 500 internal_error: Provider returned error"


def test_translate_stale_session_fixture() -> None:
    """Real bogus-``--resume`` capture: a single error result, no
    thread.started (the requested session was never opened)."""
    adapter = ClaudeAdapter()
    adapter.resume_argv("next", "00000000-0000-0000-0000-000000000000")
    events = _translate_all(_lines(FIXTURES / "stale_session.jsonl"), adapter)
    types = [e["type"] for e in events]
    assert "thread.started" not in types
    assert "error" in types
    assert types[-1] == "turn.failed"
    assert "No conversation found" in events[-1]["error"]["message"]
    assert adapter.extract_session_id(events) is None


def test_translate_stale_resume_mismatch() -> None:
    """Defensive guard: a different session_id than the requested
    ``--resume`` id suppresses thread.started and fails the turn."""
    adapter = ClaudeAdapter()
    adapter.resume_argv("next", "sess_requested")
    events = _translate_all(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "sess_other",
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": "sess_other",
                    "usage": {},
                }
            ),
        ],
        adapter,
    )
    types = [e["type"] for e in events]
    assert "thread.started" not in types
    assert "error" in types
    mismatch = [e for e in events if e["type"] == "error"][0]
    assert "sess_requested" in mismatch["message"]
    assert "sess_other" in mismatch["message"]
    assert types[-1] == "turn.failed"
    assert adapter.extract_session_id(events) is None


def test_translate_matching_resume_id() -> None:
    adapter = ClaudeAdapter()
    adapter.resume_argv("next", SESS_ID)
    events = _translate_all(_lines(FIXTURES / "resume.jsonl"), adapter)
    started = [e for e in events if e["type"] == "thread.started"]
    assert started == [{"type": "thread.started", "thread_id": SESS_ID}]
    assert events[-1]["type"] == "turn.completed"


def test_fixture_lines_all_translate() -> None:
    """Every staged line yields >=1 event except the intentional bad line."""
    for fixture in FIXTURES.glob("*.jsonl"):
        adapter = ClaudeAdapter()
        for line in _lines(fixture):
            if not line.strip():
                continue
            events = adapter.translate(line)
            if "not json" in line:
                assert events == []
            else:
                assert events, f"{fixture.name}: {line[:80]}"


def test_translate_non_object_lines_return_empty() -> None:
    """Only lines with no JSON object return [] (runner counts them bad)."""
    adapter = ClaudeAdapter()
    assert adapter.translate("this is not json") == []
    assert adapter.translate("") == []
    assert adapter.translate("[1,2,3]") == []


def test_translate_unknown_kinds_are_noop() -> None:
    """SOR-80: parseable objects never return []; forward-compatible kinds
    (stream_event partials, hook events, system subtypes) are NOOP."""
    adapter = ClaudeAdapter()
    assert adapter.translate('{"type": "mystery"}') == [{"type": NOOP_EVENT_TYPE}]
    assert adapter.translate('{"type": "system", "subtype": "compact_boundary"}') == [
        {"type": NOOP_EVENT_TYPE}
    ]
    assert adapter.translate('{"type": "stream_event", "event": {"type": "x"}}') == [
        {"type": NOOP_EVENT_TYPE}
    ]


def test_translate_session_marker_from_any_line() -> None:
    """session_id on any non-terminal-error line is the marker (rule 1)."""
    adapter = ClaudeAdapter()
    assert adapter.translate('{"type": "mystery", "session_id": "s1"}') == [
        {"type": "thread.started", "thread_id": "s1"}
    ]


def test_tool_result_without_open_call_emits_pair() -> None:
    """A first-sight tool_result emits started+completed (agy precedent)."""
    adapter = ClaudeAdapter()
    events = _translate_all(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
            json.dumps(
                {
                    "type": "user",
                    "session_id": "s1",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_x",
                                "content": "boom",
                                "is_error": True,
                            }
                        ],
                    },
                }
            ),
        ],
        adapter,
    )
    started = [e for e in events if e["type"] == "item.started"]
    completed = [e for e in events if e["type"] == "item.completed"]
    assert [i["item"]["id"] for i in started] == ["toolu_x"]
    assert [i["item"]["id"] for i in completed] == ["toolu_x"]
    assert completed[0]["item"]["status"] == "failed"
    assert completed[0]["item"]["aggregated_output"] == "boom"


def test_extract_session_id() -> None:
    adapter = ClaudeAdapter()
    events = _translate_all(_lines(FIXTURES / "success.jsonl"), adapter)
    assert adapter.extract_session_id(events) == SESS_ID
    assert adapter.extract_session_id([{"type": "turn.started"}]) is None


def test_health_from() -> None:
    adapter = ClaudeAdapter()
    assert adapter.health_from(0, "") == "ok"
    assert adapter.health_from(1, "Not logged in · Please run /login") == "auth_invalid"
    assert adapter.health_from(1, "Error: 401 Unauthorized") == "auth_invalid"
    assert adapter.health_from(1, "claude auth_invalid scenario") == "auth_invalid"
    assert adapter.health_from(1, "HTTP 429 too many requests") == "rate_limited"
    assert adapter.health_from(1, "rate limit exceeded") == "rate_limited"
    assert adapter.health_from(1, "overloaded_error") == "rate_limited"
    # Stale --resume id is a session error, not account auth -> unknown.
    assert adapter.health_from(1, "No conversation found with session ID: x") == "unknown"
    assert adapter.health_from(1, "something else exploded") == "unknown"
    assert adapter.health_from(None, "") == "unknown"

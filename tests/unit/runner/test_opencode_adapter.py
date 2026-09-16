"""OpencodeAdapter unit tests: argv, prepare_home, translate, health (SOR-96).

Translate coverage uses both the staged real-shape captures
(``tests/unit/runner/fixtures/opencode/*.jsonl`` — ``opencode run --format
json`` stream shape: per-line ``sessionID``, ``step_start``,
``reasoning``/``text`` part snapshots, ``tool_use`` ``state.status``
pending->running->completed transitions, ``step_finish`` with
``reason: tool-calls`` intermediate boundaries and accumulated ``tokens``,
``error{error:{data:{message}}}``) and the WP0 hand-written fixtures
(``tests/fixtures/events/opencode/`` — single terminal ``tool_use`` line,
flat ``sessionID`` markers).
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from runtime.runner.adapter import get_adapter
from runtime.runner.adapters.opencode import OPENCODE_AUTH_REL, OpencodeAdapter
from runtime.runner.constants import NOOP_EVENT_TYPE

ROOT = Path(__file__).resolve().parents[3]
REAL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "opencode"
WP0_FIXTURES = ROOT / "tests" / "fixtures" / "events" / "opencode"

MODEL = "openai/gpt-5.6-luna"
REAL_ID = "ses_01a0a2d787bc7192b642ad17"
WP0_ID = "ses_01a09b11abcd"


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _translate_all(lines: list[str], adapter: OpencodeAdapter | None = None) -> list[dict]:
    adapter = adapter or OpencodeAdapter()
    out: list[dict] = []
    for line in lines:
        out.extend(adapter.translate(line))
    return [e for e in out if e.get("type") != NOOP_EVENT_TYPE]


def test_registered() -> None:
    adapter = get_adapter("opencode")
    assert isinstance(adapter, OpencodeAdapter)
    assert adapter.provider == "opencode"


def test_credential_files_single_auth_json() -> None:
    assert OpencodeAdapter().credential_files == (".local/share/opencode/auth.json",)


def test_first_turn_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    monkeypatch.setenv("SBX_WORK", str(tmp_path))
    argv = OpencodeAdapter().first_turn_argv("do the thing", MODEL)
    assert argv == [
        "opencode",
        "run",
        "do the thing",
        "--format",
        "json",
        "-m",
        MODEL,
        "--dir",
        str(tmp_path),
        "--auto",
    ]


def test_first_turn_argv_omits_empty_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    monkeypatch.setenv("SBX_WORK", str(tmp_path))
    argv = OpencodeAdapter().first_turn_argv("hi", "")
    assert "-m" not in argv
    assert argv[argv.index("run") + 1] == "hi"


def test_opencode_bin_py_gets_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_BIN", "/opt/fakes/fake_opencode.py")
    argv = OpencodeAdapter().first_turn_argv("hi", MODEL)
    assert argv[:2] == [sys.executable, "/opt/fakes/fake_opencode.py"]


def _write_session(work: Path, **fields: object) -> None:
    work.mkdir(parents=True, exist_ok=True)
    (work / "session.json").write_text(json.dumps(fields), encoding="utf-8")


def test_resume_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    monkeypatch.setenv("SBX_WORK", str(tmp_path))
    _write_session(tmp_path, model=MODEL)
    argv = OpencodeAdapter().resume_argv("next please", "ses_xyz")
    assert argv == [
        "opencode",
        "run",
        "next please",
        "--format",
        "json",
        "--session",
        "ses_xyz",
        "-m",
        MODEL,
        "--dir",
        str(tmp_path),
        "--auto",
    ]


def test_resume_argv_without_session_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENCODE_BIN", raising=False)
    monkeypatch.setenv("SBX_WORK", str(tmp_path))
    argv = OpencodeAdapter().resume_argv("next", "ses_xyz")
    assert argv[argv.index("--session") + 1] == "ses_xyz"
    assert "-m" not in argv


def test_prepare_home_safe_permissions(tmp_path: Path) -> None:
    auth = tmp_path / OPENCODE_AUTH_REL
    auth.parent.mkdir(parents=True)
    auth.write_text(
        '{"anthropic":{"type":"oauth","access":"REDACTED","refresh":"REDACTED"}}\n',
        encoding="utf-8",
    )
    auth.chmod(0o644)
    OpencodeAdapter().prepare_home(tmp_path, MODEL)
    data_dir = tmp_path / ".local" / "share" / "opencode"
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    # Content is never rewritten; only the mode is tightened.
    assert "REDACTED" in auth.read_text(encoding="utf-8")
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600


def test_prepare_home_without_credential(tmp_path: Path) -> None:
    OpencodeAdapter().prepare_home(tmp_path, MODEL)
    data_dir = tmp_path / ".local" / "share" / "opencode"
    assert data_dir.is_dir()
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert not (data_dir / "auth.json").exists()


def test_translate_real_success_fixture() -> None:
    events = _translate_all(_lines(REAL_FIXTURES / "success.jsonl"))
    types = [e["type"] for e in events]
    assert types[0] == "thread.started"
    assert events[0]["thread_id"] == REAL_ID
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
    # callID is the item id; pending/running/completed updates collapse to
    # one started+completed pair.
    assert started["call_0001"]["type"] == "command_execution"
    assert started["call_0001"]["command"] == "printf 'spike-marker-001\\n' > marker.txt"
    assert completed["call_0001"]["exit_code"] == 0
    assert completed["call_0001"]["status"] == "completed"
    assert started["call_0002"]["type"] == "file_change"
    assert completed["call_0002"]["changes"][0]["path"] == "/work/marker.txt"

    reasoning = [i for i in items if i["type"] == "reasoning"]
    assert [r["text"] for r in reasoning] == ["I will write marker.txt and verify it."]
    messages = [i for i in items if i["type"] == "agent_message"]
    assert [m["text"] for m in messages] == ["Created marker.txt in the workspace."]

    # Canonical usage accumulates across both step_finish parts
    # (tool-calls intermediate + stop terminal).
    usage = events[-1]["usage"]
    assert usage["input_tokens"] == 11200
    assert usage["output_tokens"] == 42
    assert usage["reasoning_output_tokens"] == 10
    assert usage["cached_input_tokens"] == 2000
    assert usage["cache_write_input_tokens"] == 500


def test_translate_real_tool_status_transitions_single_started() -> None:
    """pending -> running -> completed collapses to exactly one
    item.started + one item.completed for the call."""
    events = _translate_all(_lines(REAL_FIXTURES / "success.jsonl"))
    started = [e for e in events if e["type"] == "item.started" and e["item"]["id"] == "call_0001"]
    completed = [
        e for e in events if e["type"] == "item.completed" and e["item"]["id"] == "call_0001"
    ]
    assert len(started) == 1
    assert len(completed) == 1


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
    assert events[-1]["usage"]["cached_input_tokens"] == 4000


def test_translate_real_nonzero_fixture() -> None:
    events = _translate_all(_lines(REAL_FIXTURES / "nonzero.jsonl"))
    types = [e["type"] for e in events]
    assert "error" in types
    assert types[-1] == "turn.failed"
    assert events[-1]["error"]["message"] == "Provider returned error"


def test_translate_real_auth_invalid_fixture() -> None:
    events = _translate_all(_lines(REAL_FIXTURES / "auth_invalid.jsonl"))
    assert events[-1]["type"] == "turn.failed"
    assert events[-1]["error"]["message"] == "Incorrect API key provided"
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "Incorrect API key" in errors[0]["message"]


def test_real_fixture_lines_all_translate() -> None:
    """Every real-shape capture line yields >=1 event (NOOP counts)."""
    for fixture in REAL_FIXTURES.glob("*.jsonl"):
        adapter = OpencodeAdapter()
        for line in _lines(fixture):
            if not line.strip():
                continue
            events = adapter.translate(line)
            assert events, f"{fixture.name}: {line[:80]}"


def test_translate_wp0_success_fixture() -> None:
    """WP0 fake shape: first-sight terminal tool_use emits started+completed."""
    events = _translate_all(_lines(WP0_FIXTURES / "success.jsonl"))
    assert events[0] == {"type": "thread.started", "thread_id": WP0_ID}
    types = [e["type"] for e in events]
    assert types[1] == "turn.started"
    items = [e["item"] for e in events if isinstance(e.get("item"), dict)]
    kinds = {i["type"] for i in items}
    assert {"reasoning", "command_execution", "agent_message"} <= kinds
    command = [i for i in items if i["type"] == "command_execution"]
    assert command[0]["command"] == "printf 'hello from fake_opencode\\n' > hello.txt"
    assert command[0]["exit_code"] == 0
    assert events[-1]["type"] == "turn.completed"
    usage = events[-1]["usage"]
    assert usage["input_tokens"] == 14290
    assert usage["output_tokens"] == 6
    assert usage["reasoning_output_tokens"] == 4
    assert usage["cached_input_tokens"] == 8113
    assert usage["cache_write_input_tokens"] == 0


def test_translate_wp0_error_object() -> None:
    events = _translate_all(_lines(WP0_FIXTURES / "auth_invalid.jsonl"))
    assert events[-1]["type"] == "turn.failed"
    assert events[-1]["error"]["message"] == "Incorrect API key provided"


def test_wp0_fixture_lines_all_translate() -> None:
    """Every WP0 fixture line yields >=1 event except the intentional bad line."""
    for fixture in WP0_FIXTURES.glob("*.jsonl"):
        adapter = OpencodeAdapter()
        for line in _lines(fixture):
            if not line.strip():
                continue
            events = adapter.translate(line)
            if "not json" in line:
                assert events == []
            else:
                assert events, f"{fixture.name}: {line[:80]}"


def test_translate_stale_resume_mismatch() -> None:
    """sessionID != requested --session id -> error, no thread.started."""
    adapter = OpencodeAdapter()
    adapter.resume_argv("next", "ses_requested")
    events = _translate_all(
        [
            json.dumps(
                {
                    "type": "step_start",
                    "sessionID": "ses_other",
                    "part": {"id": "prt_0", "sessionID": "ses_other"},
                }
            ),
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "ses_other",
                    "part": {"reason": "stop", "tokens": {}},
                }
            ),
        ],
        adapter,
    )
    types = [e["type"] for e in events]
    assert "thread.started" not in types
    assert "error" in types
    mismatch = [e for e in events if e["type"] == "error"][0]
    assert "ses_requested" in mismatch["message"]
    assert "ses_other" in mismatch["message"]
    assert types[-1] == "turn.failed"
    assert adapter.extract_session_id(events) is None


def test_translate_matching_resume_id() -> None:
    adapter = OpencodeAdapter()
    adapter.resume_argv("next", REAL_ID)
    events = _translate_all(_lines(REAL_FIXTURES / "resume.jsonl"), adapter)
    started = [e for e in events if e["type"] == "thread.started"]
    assert started == [{"type": "thread.started", "thread_id": REAL_ID}]
    assert events[-1]["type"] == "turn.completed"


def test_translate_non_object_lines_return_empty() -> None:
    """Only lines with no JSON object return [] (runner counts them bad)."""
    adapter = OpencodeAdapter()
    assert adapter.translate("this is not json") == []
    assert adapter.translate("") == []
    assert adapter.translate("[1,2,3]") == []


def test_translate_unknown_kinds_are_noop() -> None:
    """SOR-80: parseable objects never return []; forward-compatible kinds
    (message updates, permission asks, file edits) are acknowledged NOOP."""
    adapter = OpencodeAdapter()
    assert adapter.translate('{"type": "mystery"}') == [{"type": NOOP_EVENT_TYPE}]
    # A sessionID-bearing line still emits thread.started first (rule 1);
    # the unknown kind itself maps to no canonical event.
    assert adapter.translate('{"type": "message.updated", "sessionID": "s"}') == [
        {"type": "thread.started", "thread_id": "s"}
    ]
    assert adapter.translate('{"type": "permission.asked", "id": "p1"}') == [
        {"type": NOOP_EVENT_TYPE}
    ]


def test_translate_step_finish_terminal_reason_failed() -> None:
    """A non-stop, non-tool-calls finish reason (e.g. ``error``) fails the
    turn instead of reporting success."""
    events = _translate_all(
        [
            json.dumps({"type": "step_start", "sessionID": "ses_x"}),
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "ses_x",
                    "part": {"reason": "error", "tokens": {"input": 3}},
                }
            ),
        ]
    )
    assert events[-1]["type"] == "turn.failed"
    assert "reason error" in events[-1]["error"]["message"]


def test_extract_session_id() -> None:
    adapter = OpencodeAdapter()
    events = _translate_all(_lines(REAL_FIXTURES / "success.jsonl"), adapter)
    assert adapter.extract_session_id(events) == REAL_ID
    assert adapter.extract_session_id([{"type": "turn.started"}]) is None


def test_health_from_stream_errors_when_stderr_clean() -> None:
    """Real ``--format json`` shape: fatal errors land only on the stdout
    ``error`` event; stderr has no needles. health_from must classify from
    the translated stream (SOR-96 review fix)."""
    adapter = OpencodeAdapter()
    _translate_all(_lines(REAL_FIXTURES / "auth_invalid.jsonl"), adapter)
    assert adapter.health_from(1, "") == "auth_invalid"

    rate_limited = OpencodeAdapter()
    _translate_all(
        [
            json.dumps(
                {
                    "type": "error",
                    "sessionID": "s",
                    "error": {"name": "APIError", "data": {"message": "429 rate limit"}},
                }
            )
        ],
        rate_limited,
    )
    assert rate_limited.health_from(1, "") == "rate_limited"
    # Stream errors never override a clean exit.
    assert rate_limited.health_from(0, "") == "ok"


def test_health_from_stale_stream_stays_unknown() -> None:
    """Adapter-generated stale-resume errors are not provider auth signals —
    they must not feed the stream fallback."""
    adapter = OpencodeAdapter()
    adapter.resume_argv("next", "ses_requested")
    _translate_all(
        [
            json.dumps({"type": "step_start", "sessionID": "ses_other"}),
            json.dumps(
                {
                    "type": "step_finish",
                    "sessionID": "ses_other",
                    "part": {"reason": "stop", "tokens": {}},
                }
            ),
        ],
        adapter,
    )
    assert adapter.health_from(1, "") == "unknown"


def test_health_from() -> None:
    adapter = OpencodeAdapter()
    assert adapter.health_from(0, "") == "ok"
    assert adapter.health_from(1, "Incorrect API key provided") == "auth_invalid"
    assert adapter.health_from(1, "Error: 401 Unauthorized") == "auth_invalid"
    assert adapter.health_from(1, "opencode auth_invalid scenario") == "auth_invalid"
    assert adapter.health_from(1, "HTTP 429 too many requests") == "rate_limited"
    assert adapter.health_from(1, "RESOURCE_EXHAUSTED: quota") == "rate_limited"
    assert adapter.health_from(1, "rate limit exceeded") == "rate_limited"
    # Stale --session id is a session error, not account auth -> unknown.
    assert adapter.health_from(1, 'Error: Session "ses_x" not found') == "unknown"
    assert adapter.health_from(1, "something else exploded") == "unknown"
    assert adapter.health_from(None, "") == "unknown"

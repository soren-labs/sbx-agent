"""AntigravityAdapter unit tests: argv, prepare_home, translate, health (SOR-62).

Translate coverage uses both the staged real SOR-60 captures
(``tests/unit/runner/fixtures/antigravity/*.jsonl`` — top-level ``conversation_id``,
``step_type`` user_input/agent_response/tool/system_message, string
``result.error``) and the WP0 hand-written fixtures
(``tests/fixtures/events/antigravity/`` — nested ``init.conversation_id``,
``thinking``/``tool_call`` step types, object ``result.error``).
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from runtime.runner.adapter import get_adapter
from runtime.runner.adapters.antigravity import (
    OAUTH_TOKEN_REL,
    ONBOARDING_STATE,
    ONBOARDING_STATE_REL,
    AntigravityAdapter,
)
from runtime.runner.constants import NOOP_EVENT_TYPE

ROOT = Path(__file__).resolve().parents[3]
REAL_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "antigravity"
WP0_FIXTURES = ROOT / "tests" / "fixtures" / "events" / "antigravity"

MODEL = "gemini-3.8-flash-low"
REAL_ID = "4e9eadd6-eb70-442b-b24a-1660db079181"
WP0_ID = "c3b66b04-872b-4fbe-a3a4-058a026ef20a"


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _translate_all(lines: list[str], adapter: AntigravityAdapter | None = None) -> list[dict]:
    adapter = adapter or AntigravityAdapter()
    out: list[dict] = []
    for line in lines:
        out.extend(adapter.translate(line))
    return [e for e in out if e.get("type") != NOOP_EVENT_TYPE]


def test_registered() -> None:
    adapter = get_adapter("antigravity")
    assert isinstance(adapter, AntigravityAdapter)
    assert adapter.provider == "antigravity"


def test_credential_files_portable_bundle() -> None:
    # SOR-258: token plus the non-secret onboarding marker the 1.2.x CLI
    # requires next to it on a fresh HOME.
    assert AntigravityAdapter().credential_files == (
        ".gemini/antigravity-cli/antigravity-oauth-token",
        ".gemini/antigravity-cli/cache/onboarding.json",
    )


def test_first_turn_argv_matches_spike(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGY_BIN", raising=False)
    argv = AntigravityAdapter().first_turn_argv("do the thing", MODEL)
    assert argv == [
        "agy",
        "-p",
        "do the thing",
        "--output-format",
        "stream-json",
        "--model",
        MODEL,
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
    ]


def test_first_turn_argv_omits_empty_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGY_BIN", raising=False)
    argv = AntigravityAdapter().first_turn_argv("hi", "")
    assert "--model" not in argv


def test_agy_bin_py_gets_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGY_BIN", "/opt/fakes/fake_agy.py")
    argv = AntigravityAdapter().first_turn_argv("hi", MODEL)
    assert argv[:2] == [sys.executable, "/opt/fakes/fake_agy.py"]


def _write_session(work: Path, **fields: object) -> None:
    work.mkdir(parents=True, exist_ok=True)
    (work / "session.json").write_text(json.dumps(fields), encoding="utf-8")


def test_resume_argv_matches_spike(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGY_BIN", raising=False)
    monkeypatch.setenv("SBX_WORK", str(tmp_path))
    _write_session(tmp_path, model=MODEL)
    argv = AntigravityAdapter().resume_argv("next please", "conv-xyz")
    assert argv == [
        "agy",
        "-p",
        "next please",
        "--conversation",
        "conv-xyz",
        "--output-format",
        "stream-json",
        "--model",
        MODEL,
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
    ]


def test_resume_argv_without_session_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGY_BIN", raising=False)
    monkeypatch.setenv("SBX_WORK", str(tmp_path))
    argv = AntigravityAdapter().resume_argv("next", "conv-xyz")
    assert argv[argv.index("--conversation") + 1] == "conv-xyz"
    assert "--model" not in argv


def test_prepare_home_safe_permissions(tmp_path: Path) -> None:
    token = tmp_path / OAUTH_TOKEN_REL
    token.parent.mkdir(parents=True)
    token.write_text('{"auth_method":"consumer","id_token":"REDACTED"}\n', encoding="utf-8")
    token.chmod(0o644)
    AntigravityAdapter().prepare_home(tmp_path, MODEL)
    assert stat.S_IMODE((tmp_path / ".gemini").stat().st_mode) == 0o700
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    # Content is never rewritten; only the mode is tightened.
    assert "REDACTED" in token.read_text(encoding="utf-8")
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    # SOR-258: the missing onboarding marker is reconstructed (0600).
    marker = tmp_path / ONBOARDING_STATE_REL
    assert marker.read_text(encoding="utf-8") == ONBOARDING_STATE
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert stat.S_IMODE(marker.parent.stat().st_mode) == 0o700


def test_prepare_home_without_token(tmp_path: Path) -> None:
    AntigravityAdapter().prepare_home(tmp_path, MODEL)
    cli_dir = tmp_path / ".gemini" / "antigravity-cli"
    assert cli_dir.is_dir()
    assert not (cli_dir / "antigravity-oauth-token").exists()
    assert (cli_dir / "cache" / "onboarding.json").is_file()


def test_prepare_home_keeps_restored_marker(tmp_path: Path) -> None:
    """A marker restored from the bundle is never overwritten (SOR-258)."""
    marker = tmp_path / ONBOARDING_STATE_REL
    marker.parent.mkdir(parents=True)
    marker.write_text('{"onboardingComplete":true,"custom":1}\n', encoding="utf-8")
    AntigravityAdapter().prepare_home(tmp_path, MODEL)
    assert marker.read_text(encoding="utf-8") == '{"onboardingComplete":true,"custom":1}\n'
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_translate_real_success_fixture() -> None:
    events = _translate_all(_lines(REAL_FIXTURES / "success.jsonl"))
    types = [e["type"] for e in events]
    assert types[0] == "thread.started"
    assert events[0]["thread_id"] == REAL_ID
    assert "turn.started" in types  # user_input step
    assert types[-1] == "turn.completed"

    items = [e["item"] for e in events if isinstance(e.get("item"), dict)]
    kinds = {i["type"] for i in items}
    assert {"command_execution", "file_change", "agent_message"} <= kinds

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
    cmd_started = [i for i in started.values() if i["type"] == "command_execution"]
    cmd_done = [i for i in completed.values() if i["type"] == "command_execution"]
    assert cmd_started and cmd_started[0]["command"] == "pwd"
    assert cmd_started[0]["exit_code"] is None
    assert cmd_done and cmd_done[0]["command"] == "pwd"
    assert cmd_done[0]["aggregated_output"] == "/work/home\r\n"
    assert cmd_done[0]["exit_code"] == 0
    assert cmd_done[0]["status"] == "completed"
    # ACTIVE and DONE updates of one step share the item id.
    assert cmd_done[0]["id"] == cmd_started[0]["id"]

    file_items = [i for i in completed.values() if i["type"] == "file_change"]
    assert file_items and file_items[0]["changes"][0]["path"] == "/work/home/marker.txt"

    messages = [i for i in completed.values() if i["type"] == "agent_message"]
    # text_delta fragments across ACTIVE+DONE concatenate into one item.
    assert [m["text"] for m in messages] == ["DONE\n"]

    usage = events[-1]["usage"]
    assert usage["input_tokens"] == 26610
    assert usage["cached_input_tokens"] == 24406
    assert usage["output_tokens"] == 393
    assert usage["reasoning_output_tokens"] == 258
    assert usage["cache_write_input_tokens"] == 0


def test_translate_real_resume_fixture() -> None:
    events = _translate_all(_lines(REAL_FIXTURES / "resume.jsonl"))
    assert events[0] == {"type": "thread.started", "thread_id": REAL_ID}
    messages = [
        e["item"]
        for e in events
        if e["type"] == "item.completed"
        and isinstance(e.get("item"), dict)
        and e["item"]["type"] == "agent_message"
    ]
    assert messages[0]["text"] == "FILE=marker.txt CONTENT=spike-marker-001\n"
    assert events[-1]["type"] == "turn.completed"
    assert events[-1]["usage"]["cached_input_tokens"] == 36600


def test_translate_real_nonzero_fixture() -> None:
    events = _translate_all(_lines(REAL_FIXTURES / "nonzero.jsonl"))
    assert events[-1]["type"] == "turn.failed"
    assert events[-1]["error"]["message"].startswith("invalid model selection")


def test_translate_real_auth_invalid_fixture() -> None:
    events = _translate_all(_lines(REAL_FIXTURES / "auth_invalid.jsonl"))
    assert events[-1]["type"] == "turn.failed"
    assert events[-1]["error"]["message"] == "authentication failed or timed out"


def test_translate_wp0_success_fixture_nested_id() -> None:
    """WP0 fixture shape: conversation_id nested in init, dict error, etc."""
    events = _translate_all(_lines(WP0_FIXTURES / "success.jsonl"))
    assert events[0] == {"type": "thread.started", "thread_id": WP0_ID}
    items = [e["item"] for e in events if isinstance(e.get("item"), dict)]
    kinds = {i["type"] for i in items}
    assert {"reasoning", "command_execution", "agent_message"} <= kinds
    cmd = [i for i in items if i["type"] == "command_execution"]
    assert cmd[0]["command"] == "printf 'hello from fake_agy\\n' > hello.txt"
    assert events[-1]["type"] == "turn.completed"
    assert events[-1]["usage"]["cached_input_tokens"] == 8113


def test_translate_wp0_error_object() -> None:
    events = _translate_all(_lines(WP0_FIXTURES / "auth_invalid.jsonl"))
    assert events[-1]["type"] == "turn.failed"
    assert events[-1]["error"]["message"] == "authentication failed: token expired"


def test_wp0_fixture_lines_all_translate() -> None:
    """Every WP0 fixture line yields >=1 event except the intentional bad line."""
    for fixture in WP0_FIXTURES.glob("*.jsonl"):
        adapter = AntigravityAdapter()
        for line in _lines(fixture):
            if not line.strip():
                continue
            events = adapter.translate(line)
            if "not json" in line:
                assert events == []
            else:
                assert events, f"{fixture.name}: {line[:80]}"


def test_translate_result_response_fallback() -> None:
    """A SUCCESS result emits a fallback agent_message when no delta was seen."""
    adapter = AntigravityAdapter()
    line = json.dumps(
        {
            "event": "result",
            "result": {
                "conversation_id": "conv-1",
                "status": "SUCCESS",
                "response": "final answer",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        }
    )
    events = _translate_all([line], adapter)
    assert events[0] == {"type": "thread.started", "thread_id": "conv-1"}
    assert events[1]["item"]["text"] == "final answer"
    assert events[-1]["type"] == "turn.completed"


def test_translate_stale_resume_mismatch() -> None:
    """init.conversation_id != requested --conversation id -> error, no thread."""
    adapter = AntigravityAdapter()
    adapter.resume_argv("next", "requested-id")
    events = _translate_all(
        [
            json.dumps(
                {
                    "event": "init",
                    "conversation_id": "new-id",
                    "init": {"model": MODEL, "cwd": "/work", "tools": []},
                }
            ),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "new-id",
                        "status": "SUCCESS",
                        "response": "ok",
                        "usage": {},
                    },
                }
            ),
        ],
        adapter,
    )
    types = [e["type"] for e in events]
    assert "thread.started" not in types
    assert "error" in types
    assert "requested-id" in events[0]["message"]
    assert "new-id" in events[0]["message"]
    assert types[-1] == "turn.failed"
    assert adapter.extract_session_id(events) is None


def test_translate_matching_resume_id() -> None:
    adapter = AntigravityAdapter()
    adapter.resume_argv("next", REAL_ID)
    events = _translate_all(_lines(REAL_FIXTURES / "resume.jsonl"), adapter)
    assert events[0] == {"type": "thread.started", "thread_id": REAL_ID}
    assert events[-1]["type"] == "turn.completed"


def test_translate_non_object_lines_return_empty() -> None:
    """Only lines with no JSON object return [] (runner counts them bad)."""
    adapter = AntigravityAdapter()
    assert adapter.translate("this is not json") == []
    assert adapter.translate("") == []
    assert adapter.translate("[1,2,3]") == []


def test_translate_unknown_events_are_noop() -> None:
    """SOR-80: parseable objects never return []; unknown event kinds and
    recognised kinds with unmappable payloads are acknowledged as NOOP."""
    adapter = AntigravityAdapter()
    assert adapter.translate('{"event": "mystery"}') == [{"type": NOOP_EVENT_TYPE}]
    assert adapter.translate('{"event": "step_update", "step_update": "x"}') == [
        {"type": NOOP_EVENT_TYPE}
    ]
    assert adapter.translate('{"event": "init"}') == [{"type": NOOP_EVENT_TYPE}]


def test_extract_session_id() -> None:
    adapter = AntigravityAdapter()
    events = _translate_all(_lines(REAL_FIXTURES / "success.jsonl"), adapter)
    assert adapter.extract_session_id(events) == REAL_ID
    assert adapter.extract_session_id([{"type": "turn.started"}]) is None


def test_health_from() -> None:
    adapter = AntigravityAdapter()
    assert adapter.health_from(0, "") == "ok"
    assert (
        adapter.health_from(1, "Authentication required. Please visit the URL to log in:")
        == "auth_invalid"
    )
    assert adapter.health_from(1, "antigravity auth_invalid scenario") == "auth_invalid"
    assert adapter.health_from(1, "HTTP 429 too many requests") == "rate_limited"
    assert adapter.health_from(1, "RESOURCE_EXHAUSTED: quota") == "rate_limited"
    assert adapter.health_from(1, "something else exploded") == "unknown"
    assert adapter.health_from(None, "") == "unknown"

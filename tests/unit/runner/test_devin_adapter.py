"""DevinAdapter unit tests: argv, prepare_home, translate, health (SOR-72)."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from runtime.runner.adapter import get_adapter
from runtime.runner.adapters.devin import DevinAdapter
from runtime.runner.constants import NOOP_EVENT_TYPE

FIXTURE_DIR = Path(__file__).resolve().parents[1].parent / "fixtures" / "events" / "devin"


@pytest.fixture
def cli_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBX_DEVIN_TRANSPORT", "cli")
    monkeypatch.setenv("DEVIN_BIN", "/opt/bin/devin")


def test_registered() -> None:
    adapter = get_adapter("devin")
    assert isinstance(adapter, DevinAdapter)
    assert adapter.provider == "devin"


def test_credential_files() -> None:
    assert DevinAdapter().credential_files == (".local/share/devin/credentials.toml",)


def test_cli_first_turn_argv(cli_transport: None) -> None:
    argv = DevinAdapter().first_turn_argv("create hello.txt", "swe-2-medium")
    assert argv == ["/opt/bin/devin", "-p", "create hello.txt"]


def test_cli_resume_argv(cli_transport: None) -> None:
    argv = DevinAdapter().resume_argv("update it", "devin-xyz")
    assert argv == ["/opt/bin/devin", "--resume", "devin-xyz", "-p", "update it"]


def test_acp_first_turn_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SBX_DEVIN_TRANSPORT", raising=False)
    argv = DevinAdapter().first_turn_argv("do the thing", "swe-2-high")
    assert argv[:3] == [sys.executable, "-m", "runtime.runner.adapters.devin_acp"]
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "swe-2-high"
    assert argv[-2:] == ["--", "do the thing"]
    assert "--resume" not in argv


def test_acp_resume_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SBX_DEVIN_TRANSPORT", raising=False)
    argv = DevinAdapter().resume_argv("continue", "lowly-slayer")
    assert argv[:3] == [sys.executable, "-m", "runtime.runner.adapters.devin_acp"]
    assert argv[argv.index("--resume") + 1] == "lowly-slayer"
    assert argv[-2:] == ["--", "continue"]
    assert "--model" not in argv


@pytest.mark.parametrize("model", ["swe-2-medium", "swe-2-high", "swe-2-max"])
def test_prepare_home_writes_config(tmp_path: Path, model: str) -> None:
    DevinAdapter().prepare_home(tmp_path, model)
    cfg = json.loads((tmp_path / ".config" / "devin" / "config.json").read_text())
    assert cfg["agent"]["model"] == model
    assert (tmp_path / ".local" / "share" / "devin").is_dir()


def test_prepare_home_preserves_credentials_and_existing(tmp_path: Path) -> None:
    creds = tmp_path / ".local" / "share" / "devin" / "credentials.toml"
    creds.parent.mkdir(parents=True)
    creds.write_text('windsurf_api_key = "REDACTED"\n', encoding="utf-8")
    creds.chmod(0o600)
    cfg_dir = tmp_path / ".config" / "devin"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"version": 1, "theme_mode": "dark"}), encoding="utf-8"
    )
    DevinAdapter().prepare_home(tmp_path, "swe-2-max")
    assert creds.read_text(encoding="utf-8") == 'windsurf_api_key = "REDACTED"\n'
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600
    cfg = json.loads((cfg_dir / "config.json").read_text())
    assert cfg["theme_mode"] == "dark"
    assert cfg["agent"]["model"] == "swe-2-max"


def _translate_all(lines: list[str]) -> list[dict]:
    adapter = DevinAdapter()
    out: list[dict] = []
    for line in lines:
        out.extend(adapter.translate(line))
    return out


def test_translate_success_fixture() -> None:
    lines = (FIXTURE_DIR / "success.jsonl").read_text(encoding="utf-8").splitlines()
    events = _translate_all(lines)
    types = [e["type"] for e in events]
    assert types[0] == "thread.started"
    assert events[0]["thread_id"] == "devin-session-01a09b11"
    assert "turn.started" in types
    assert "item.started" in types
    assert "item.completed" in types
    assert types[-1] == "turn.completed"
    items = [e["item"] for e in events if "item" in e]
    kinds = {i["type"] for i in items}
    assert {"reasoning", "command_execution", "agent_message"} <= kinds
    cmd_items = [i for i in items if i["type"] == "command_execution"]
    started = [i for i in cmd_items if i["status"] == "in_progress"]
    done = [i for i in cmd_items if i["status"] == "completed"]
    assert started and started[0]["exit_code"] is None
    assert started[0]["command"] == "printf 'hello from fake_devin\\n' > hello.txt"
    assert done and done[0]["exit_code"] == 0
    usage = events[-1]["usage"]
    assert usage["input_tokens"] == 5200
    assert usage["cached_input_tokens"] == 3100
    assert usage["reasoning_output_tokens"] == 60
    assert usage["output_tokens"] == 140


def test_translate_file_change_tool() -> None:
    lines = [
        json.dumps(
            {
                "type": "tool_call",
                "id": "write:0",
                "tool": "write",
                "kind": "edit",
                "input": {"file_path": "/w/note.txt", "content": "x"},
            }
        ),
        json.dumps(
            {
                "type": "tool_result",
                "id": "write:0",
                "tool": "write",
                "status": "completed",
            }
        ),
    ]
    events = _translate_all(lines)
    assert events[0]["type"] == "item.started"
    assert events[0]["item"]["type"] == "file_change"
    assert events[0]["item"]["changes"][0]["path"] == "/w/note.txt"
    assert events[1]["type"] == "item.completed"
    assert events[1]["item"]["type"] == "file_change"
    assert events[1]["item"]["id"] == events[0]["item"]["id"]


def test_translate_tool_result_reuses_call_context() -> None:
    # Fixture-style tool_result without id/input inherits the open call.
    lines = [
        json.dumps({"type": "tool_call", "tool": "shell", "input": {"command": "echo hi"}}),
        json.dumps({"type": "tool_result", "tool": "shell", "exit_code": 0, "output": "hi"}),
    ]
    events = _translate_all(lines)
    assert events[1]["item"]["id"] == events[0]["item"]["id"]
    assert events[1]["item"]["command"] == "echo hi"
    assert events[1]["item"]["aggregated_output"] == "hi"


def test_translate_failures_and_errors() -> None:
    events = _translate_all(
        [
            '{"type": "turn.failed", "error": {"message": "boom"}}',
            '{"type": "error", "error": {"code": "unauthorized", "message": "401"}}',
        ]
    )
    assert events[0] == {"type": "turn.failed", "error": {"message": "boom"}}
    assert events[1] == {"type": "error", "message": "401"}


def test_translate_non_object_lines_return_empty() -> None:
    """Only lines with no JSON object return [] (runner counts them bad)."""
    adapter = DevinAdapter()
    assert adapter.translate("this is not json") == []
    assert adapter.translate("") == []
    assert adapter.translate("[1,2,3]") == []


def test_translate_unknown_types_are_noop() -> None:
    """SOR-80: parseable objects never return []; unknown types and known
    types with unmappable payloads are acknowledged as NOOP."""
    adapter = DevinAdapter()
    assert adapter.translate('{"type": "mystery"}') == [{"type": NOOP_EVENT_TYPE}]
    assert adapter.translate('{"type": "session.started"}') == [{"type": NOOP_EVENT_TYPE}]
    assert adapter.translate('{"type": "assistant_message"}') == [{"type": NOOP_EVENT_TYPE}]


def test_extract_session_id() -> None:
    adapter = DevinAdapter()
    events = _translate_all(
        ['{"type": "session.started", "session_id": "abc-123", "model": "swe-2-medium"}']
    )
    assert adapter.extract_session_id(events) == "abc-123"
    assert adapter.extract_session_id([{"type": "turn.started"}]) is None


def test_health_from() -> None:
    adapter = DevinAdapter()
    assert adapter.health_from(0, "") == "ok"
    assert adapter.health_from(1, "401 Unauthorized: invalid or expired API key") == "auth_invalid"
    assert adapter.health_from(1, "not logged in; run devin auth login") == "auth_invalid"
    assert adapter.health_from(1, "HTTP 429 too many requests") == "rate_limited"
    assert adapter.health_from(1, "concurrency limit reached for account") == "rate_limited"
    assert adapter.health_from(1, "something else exploded") == "unknown"
    assert adapter.health_from(None, "") == "unknown"


def test_fixture_lines_translate_cleanly() -> None:
    """Every fixture line (except the intentional badjson line) translates."""
    for fixture in FIXTURE_DIR.glob("*.jsonl"):
        adapter = DevinAdapter()
        for line in fixture.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            events = adapter.translate(line)
            if fixture.name == "badjson.jsonl" and "not json" in line:
                assert events == []
            else:
                assert events, f"{fixture.name}: {line[:80]}"

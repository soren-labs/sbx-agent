"""Event parser: fake fixtures + real_multiturn.jsonl replay."""

from __future__ import annotations

import json
from pathlib import Path

from runtime.runner.constants import USAGE_FIELDS
from runtime.runner.events import TurnState, parse_event_line, redact_line, redact_obj
from tests.unit.runner.conftest import DEFAULT_THREAD


def test_parse_bad_line_counts_and_continues() -> None:
    state = TurnState()
    _, bad1 = state.consume_line('{"type":"thread.started","thread_id":"abc"}')
    _, bad2 = state.consume_line("this is not json")
    _, bad3 = state.consume_line(
        '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}'
    )
    assert bad1 is False
    assert bad2 is True
    assert bad3 is False
    assert state.bad_json_lines == 1
    assert state.thread_id == "abc"
    assert state.last_message == "hi"


def test_redact_secret_keys_and_sk_strings() -> None:
    obj = {"type": "x", "api_key": "sk-THISLEAKEDVALUE12", "nested": {"token": "abc"}}
    redacted = redact_obj(obj)
    assert redacted["api_key"] == "REDACTED"
    assert redacted["nested"]["token"] == "REDACTED"
    line = redact_line(json.dumps({"text": "Bearer abcdef.secret"}))
    parsed = json.loads(line)
    assert "REDACTED" in parsed["text"]


def test_redact_covers_injected_token_shapes() -> None:
    """SOR-101: ``events.jsonl`` is tailed verbatim into the public SSE
    stream, so every credential shape the control plane can inject must be
    stripped from provider text — not only sk-/Bearer."""
    secrets = {
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3In0.signaturepart",
        "sbx": "sbx_" + "a1b2c3d4" * 5,
        "xai": "xai-" + "A" * 32,
        "ghp": "ghp_" + "B" * 36,
        "ghpat": "github_pat_" + "C" * 30,
        "linear": "lin_api_" + "d" * 40,
        "aws": "AKIA" + "E" * 16,
        "google": "AIza" + "F" * 35,
    }
    text = "provider 401: " + " ".join(secrets.values())
    parsed = json.loads(redact_line(json.dumps({"type": "error", "message": text})))
    for name, secret in secrets.items():
        assert secret not in parsed["message"], name
    # Non-JSON provider output is scrubbed by the same shapes.
    scrubbed = redact_line(text)
    for name, secret in secrets.items():
        assert secret not in scrubbed, name


def test_empty_line_is_not_bad_json() -> None:
    obj, bad = parse_event_line("  \n")
    assert obj is None
    assert bad is False


def test_real_multiturn_parser_fields(repo_root: Path) -> None:
    path = repo_root / "tests" / "fixtures" / "events" / "real_multiturn.jsonl"
    state = TurnState()
    n_thread = 0
    n_completed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        obj, bad = state.consume_line(line)
        assert bad is False
        if obj and obj.get("type") == "thread.started":
            n_thread += 1
        if obj and obj.get("type") == "turn.completed":
            n_completed += 1
    assert n_thread == 3
    assert n_completed == 3
    assert state.thread_id == DEFAULT_THREAD
    assert state.last_message == "DONE"
    assert set(state.usage) == set(USAGE_FIELDS)
    assert state.usage["input_tokens"] == 25996 + 26100 + 26200
    assert state.usage["cached_input_tokens"] == 22016 + 22100 + 22200
    assert state.usage["cache_write_input_tokens"] == 0
    assert state.usage["output_tokens"] == 325 + 80 + 10
    assert state.usage["reasoning_output_tokens"] == 238 + 40 + 0
    assert state.bad_json_lines == 0

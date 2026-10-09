"""Every enabled Harness against streams recorded from its official CLI running a real
tool-using Turn on a BYOK endpoint, plus the invocation/config each adapter prepares."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from runtime.harnesses.protocol import (
    INFERENCE_KEY_ENV,
    HarnessError,
    TurnContext,
)
from runtime.harnesses.registry import installed

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "harnesses"
KEY = "byok-harness-test-key-0123456789"
URLS = {
    "openai_chat": "https://inference.example.test/v1",
    "openai_responses": "https://inference.example.test/v1",
    "anthropic_messages": "https://inference.example.test/anthropic",
}
HARNESSES = installed(probe=False)
RECORDED = [
    ("codex", "success", "codex.turn.completed"),
    ("claude", "success", "claude.result"),
    ("grok", "resume", "grok.result"),
    ("commandcode", "success", "commandcode.result"),
    ("opencode", "byok_success", "opencode.step_finish"),
]


def context(tmp_path: Path, provider: str, *, prompt: str = "do it", binding=None) -> TurnContext:
    protocol = HARNESSES[provider].describe().inference_protocols[0]
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    return TurnContext(
        "sess",
        "turn",
        "exec",
        "op",
        1,
        worktree,
        tmp_path / "home",
        prompt,
        native_binding=binding,
        inference={"protocol": protocol, "base_url": URLS[protocol], "model": "vendor/model-1"},
    )


def secrets() -> dict:
    return {"inference": {"api_key": KEY}}


def replay(provider: str, name: str, ctx: TurnContext) -> tuple[list[dict], dict]:
    harness = HARNESSES[provider]
    state = harness.new_state(ctx)
    lines = (FIXTURES / provider / f"{name}.jsonl").read_text().splitlines()
    return [o for line in lines for o in harness.normalize(line, state)], state


@pytest.mark.parametrize("provider,name,usage_source", RECORDED)
def test_recorded_real_turn_normalizes_to_canonical_events(
    tmp_path, provider, name, usage_source
) -> None:
    harness = HARNESSES[provider]
    out, state = replay(provider, name, context(tmp_path, provider))
    kinds = [o["type"] for o in out]
    bound = [o["payload"] for o in out if o["type"] == "execution.native_bound"]
    assert len(bound) == 1 and bound[0]["provider_id"] == provider and bound[0]["native_id"]
    started = [o["payload"] for o in out if o["type"] == "tool.started"]
    completed = [o["payload"] for o in out if o["type"] == "tool.completed"]
    assert started, "the recorded Turn ran at least one tool"
    assert {t["tool_id"] for t in started} == {t["tool_id"] for t in completed}
    assert all(t["error"] is False and t["name"] for t in completed)
    assert any(t["output"] for t in completed), "tool output is carried, not dropped"
    assert kinds.index("tool.started") < kinds.index("tool.completed")
    text = [
        o["payload"]
        for o in out
        if o["type"].startswith("message.part") and o["payload"]["kind"] == "text"
    ]
    assert text and all(p["mode"] == "replace" and p["content"] for p in text)
    keys: dict[str, int] = {}
    for part in (o["payload"] for o in out if o["type"].startswith("message.part")):
        assert part["revision"] == keys.get(part["part_key"], 0) + 1, "revisions are gapless"
        keys[part["part_key"]] = part["revision"]
    assert "diagnostic.reported" not in kinds or all(
        o["payload"].get("category") == "cli_notice"
        for o in out
        if o["type"] == "diagnostic.reported"
    )
    usage = harness.finish(state)
    assert usage and usage[0]["payload"]["source"] == usage_source
    assert usage[0]["payload"]["input_tokens"] > 0 and usage[0]["payload"]["output_tokens"] > 0
    assert harness.classify_outcome({"exit_code": 0}, state).verdict == "success"
    assert harness.classify_outcome({"exit_code": None}, state).verdict == "unknown"
    assert harness.classify_outcome({"exit_code": 1}, state).verdict == "failure"


@pytest.mark.parametrize("provider", ["codex", "claude", "commandcode"])
def test_recorded_resume_reports_the_same_native_session(tmp_path, provider) -> None:
    first, _ = replay(provider, "success", context(tmp_path, provider))
    native = next(o["payload"]["native_id"] for o in first if o["type"] == "execution.native_bound")
    binding = {"provider_id": provider, "native_id": native}
    out, state = replay(provider, "resume", context(tmp_path, provider, binding=binding))
    bound = next(o["payload"] for o in out if o["type"] == "execution.native_bound")
    assert bound == {"provider_id": provider, "native_id": native, "resumed": True}
    assert state["mismatch"] is False
    # The same stream under a different expected session is a context mismatch, not success.
    other = {"provider_id": provider, "native_id": "some-other-session"}
    _, wrong = replay(provider, "resume", context(tmp_path, provider, binding=other))
    outcome = HARNESSES[provider].classify_outcome({"exit_code": 0}, wrong)
    assert outcome.verdict == "failure" and outcome.error_code == "context_mismatch"


@pytest.mark.parametrize("provider", sorted(HARNESSES))
def test_prepare_points_the_official_cli_at_the_endpoint_without_writing_the_key(
    tmp_path, provider
) -> None:
    harness = HARNESSES[provider]
    ctx = context(tmp_path, provider, prompt="--help me refactor")
    prepared = harness.prepare(ctx, secrets())
    invocation = harness.start_turn(ctx, prepared)
    # Isolated environment: the host contributes PATH only.
    assert invocation.env["HOME"] == str(tmp_path / "home") and invocation.cwd == ctx.worktree
    assert KEY in invocation.env.values() and prepared.secrets == [KEY]
    assert KEY not in " ".join(invocation.argv), "the key is never an argument"
    written = [p for p in (tmp_path / "home").rglob("*") if p.is_file()]
    assert all(KEY not in p.read_text() for p in written), "no credential reaches disk"
    assert prepared.credential_files == []
    config = "\n".join(p.read_text() for p in written)
    base_url = ctx.inference["base_url"]
    if provider == "claude":
        assert invocation.env["ANTHROPIC_BASE_URL"] == base_url
        assert invocation.env["ANTHROPIC_API_KEY"] == KEY
        assert invocation.env["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "home"))
        assert invocation.env["ANTHROPIC_MODEL"] == "vendor/model-1"
    else:
        assert invocation.env[INFERENCE_KEY_ENV] == KEY
        assert base_url in config and INFERENCE_KEY_ENV in config and "vendor/model-1" in config
    # A prompt that looks like an option is still delivered as the prompt.
    prompts = [a for a in invocation.argv if "help me refactor" in a]
    assert len(prompts) == 1
    assert not prompts[0].startswith("-")
    binding = {"provider_id": provider, "native_id": "native-123"}
    if provider == "grok":
        with pytest.raises(HarnessError) as missing:
            harness.resume_turn(ctx, prepared, binding)
        assert missing.value.code == "context_unavailable", "never falls through to xAI login"
        (tmp_path / "home" / ".grok" / "sessions" / "%2Fwork" / "native-123").mkdir(parents=True)
    resumed = harness.resume_turn(ctx, prepared, binding)
    assert "native-123" in resumed.argv
    with pytest.raises(HarnessError) as foreign:
        harness.resume_turn(ctx, prepared, {"provider_id": "someone-else", "native_id": "x"})
    assert foreign.value.code == "context_unavailable"


@pytest.mark.parametrize("provider", sorted(HARNESSES))
def test_prepare_refuses_missing_or_unspeakable_inference(tmp_path, provider) -> None:
    harness = HARNESSES[provider]
    ctx = context(tmp_path, provider)
    with pytest.raises(HarnessError) as missing:
        harness.prepare(ctx, {})
    assert missing.value.code == "connection_required"
    ctx.inference = {"protocol": "grpc", "base_url": "https://x.example.test", "model": "m"}
    with pytest.raises(HarnessError) as wrong:
        harness.prepare(ctx, secrets())
    assert wrong.value.code == "unsupported_capability"


def test_command_code_runs_local_only_with_a_non_secret_placeholder(tmp_path) -> None:
    harness = HARNESSES["commandcode"]
    ctx = context(tmp_path, "commandcode")
    prepared = harness.prepare(ctx, secrets())
    argv = harness.start_turn(ctx, prepared).argv
    assert prepared.env["CMD_LOCAL_ONLY"] == "1" and "--local-only" in argv
    assert prepared.env["COMMAND_CODE_API_KEY"] not in prepared.secrets
    providers = json.loads((tmp_path / "home/.commandcode/providers.json").read_text())
    assert providers["provider"]["sbx"]["apiKey"] == f"${INFERENCE_KEY_ENV}"
    assert providers["provider"]["sbx"]["api"] == "openai-completions"


def test_claude_reports_a_rejected_key_from_the_first_retry_frame() -> None:
    harness = HARNESSES["claude"]
    state = harness.new_state(TurnContext("s", "t", "e", "o", 1, Path("/w"), Path("/h"), "p"))
    retry = {
        "type": "system",
        "subtype": "api_retry",
        "attempt": 1,
        "error_status": 401,
        "error": "authentication_failed",
        "session_id": "abc",
    }
    out = harness.normalize(json.dumps(retry), state) + harness.normalize(json.dumps(retry), state)
    diagnostics = [o["payload"] for o in out if o["type"] == "diagnostic.reported"]
    assert len(diagnostics) == 1 and diagnostics[0]["credential_health"] == "invalid"
    outcome = harness.classify_outcome({"exit_code": 1}, state)
    assert (outcome.credential_health, outcome.error_code) == ("invalid", "credential_invalid")


@pytest.mark.parametrize(
    "provider,frame",
    [
        (
            "grok",
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "errors": ["Unauthorized (401) from https://inference.example.test"],
            },
        ),
        (
            "commandcode",
            {
                "type": "event",
                "event": {
                    "type": "run_error",
                    "error": {"name": "TransportError", "message": "Authentication Fails"},
                },
            },
        ),
        ("codex", {"type": "turn.failed", "error": {"message": "unexpected status 401"}}),
    ],
)
def test_recorded_auth_failure_shapes_mark_the_credential_invalid(provider, frame) -> None:
    harness = HARNESSES[provider]
    state = harness.new_state(TurnContext("s", "t", "e", "o", 1, Path("/w"), Path("/h"), "p"))
    out = harness.normalize(json.dumps(frame), state)
    assert out[-1]["payload"]["credential_health"] == "invalid"
    outcome = harness.classify_outcome({"exit_code": 1}, state)
    assert outcome.error_code == "credential_invalid"
    assert outcome.retry_advice == "replace_credential"


def test_missing_native_session_is_context_unavailable() -> None:
    cases = {
        "claude": "No conversation found with session ID: 1111",
        "commandcode": 'Error: No session "1111" found to resume.',
    }
    for provider, stderr in cases.items():
        harness = HARNESSES[provider]
        state = harness.new_state(TurnContext("s", "t", "e", "o", 1, Path("/w"), Path("/h"), "p"))
        outcome = harness.classify_outcome({"exit_code": 1, "stderr_tail": stderr}, state)
        assert outcome.error_code == "context_unavailable", provider

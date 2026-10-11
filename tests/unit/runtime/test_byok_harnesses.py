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
# Harnesses with a custom-API (bring-your-own-key) mode; the rest run on a Machine Slot only.
BYOK = sorted(p for p, h in HARNESSES.items() if h.describe().inference_protocols)
RECORDED = [
    ("codex", "success", "codex.turn.completed"),
    ("claude", "success", "claude.result"),
    ("grok", "resume", "grok.result"),
    ("commandcode", "success", "commandcode.result"),
    ("opencode", "byok_success", "opencode.step_finish"),
]


def context(tmp_path: Path, provider: str, *, prompt: str = "do it", binding=None) -> TurnContext:
    protocols = HARNESSES[provider].describe().inference_protocols
    protocol = protocols[0] if protocols else None
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
        inference={"protocol": protocol, "base_url": URLS[protocol], "model": "vendor/model-1"}
        if protocol
        else None,
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


@pytest.mark.parametrize("provider", BYOK)
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


@pytest.mark.parametrize("provider", BYOK)
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


def test_usage_reports_uncached_input_separately_from_cache_reads(tmp_path) -> None:
    """input_tokens means the same thing for every CLI: tokens that were not cache reads."""
    expected = {"codex": ("success", 17241, 8448), "commandcode": ("success", 44608, 29440)}
    for provider, (name, total_input, cached) in expected.items():
        _, state = replay(provider, name, context(tmp_path, provider))
        usage = HARNESSES[provider].finish(state)[0]["payload"]
        assert usage["cached_input_tokens"] == cached, provider
        assert usage["input_tokens"] == total_input - cached, provider


def _subscription(tmp_path: Path, **overrides) -> TurnContext:
    profile = tmp_path / "profile" / ".codex"
    profile.mkdir(parents=True, exist_ok=True)
    ctx = context(tmp_path, "codex")
    ctx.inference = {
        "mode": "subscription",
        "provider_id": "codex",
        "env": {"HOME": str(tmp_path / "profile"), "CODEX_HOME": str(profile)},
    }
    for name, value in overrides.items():
        setattr(ctx, name, value)
    return ctx


def test_codex_subscription_turn_uses_the_slot_login_and_native_effort(tmp_path) -> None:
    harness = HARNESSES["codex"]
    ctx = _subscription(tmp_path, model="vendor/model-2", effort="high")
    prepared = harness.prepare(ctx, {})
    profile = tmp_path / "profile" / ".codex"
    assert prepared.env["CODEX_HOME"] == str(profile)
    assert prepared.env["HOME"] == str(tmp_path / "home"), "the Session home stays private"
    assert INFERENCE_KEY_ENV not in prepared.env and prepared.secrets == []
    assert list(profile.iterdir()) == [], "nothing is written into the login profile"
    argv = harness.start_turn(ctx, prepared).argv
    assert argv[argv.index("-m") + 1] == "vendor/model-2"
    overrides = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]
    assert overrides == ['cli_auth_credentials_store="file"', 'model_reasoning_effort="high"']
    resumed = harness.resume_turn(ctx, prepared, {"provider_id": "codex", "native_id": "t-1"}).argv
    assert 'model_reasoning_effort="high"' in resumed and "t-1" in resumed
    assert harness.native_state_paths(prepared.home) == [], "no profile file is ever exported"


def test_codex_subscription_turn_without_effort_or_model_sends_neither(tmp_path) -> None:
    harness = HARNESSES["codex"]
    ctx = _subscription(tmp_path)
    argv = harness.start_turn(ctx, harness.prepare(ctx, {})).argv
    assert "-m" not in argv and not any("model_reasoning_effort" in a for a in argv)


def test_codex_subscription_turn_never_falls_back_to_an_api_key(tmp_path) -> None:
    harness = HARNESSES["codex"]
    ctx = _subscription(tmp_path)
    prepared = harness.prepare(ctx, secrets())
    assert INFERENCE_KEY_ENV not in prepared.env and KEY not in str(prepared.env)
    missing = _subscription(tmp_path)
    missing.inference["env"]["CODEX_HOME"] = str(tmp_path / "not-mounted")
    with pytest.raises(HarnessError) as err:
        harness.prepare(missing, secrets())
    assert err.value.code == "connection_required"


def _grok_subscription(tmp_path: Path, **overrides) -> TurnContext:
    grok_home = tmp_path / "profile" / ".grok"
    grok_home.mkdir(parents=True, exist_ok=True)
    ctx = context(tmp_path, "grok")
    ctx.inference = {
        "mode": "subscription",
        "provider_id": "grok",
        "env": {"HOME": str(tmp_path / "profile"), "GROK_HOME": str(grok_home)},
    }
    for name, value in overrides.items():
        setattr(ctx, name, value)
    return ctx


def test_grok_subscription_turn_uses_the_slot_login_model_and_effort(tmp_path) -> None:
    harness = HARNESSES["grok"]
    ctx = _grok_subscription(tmp_path, model="grok-a", effort="xhigh")
    prepared = harness.prepare(ctx, secrets())
    grok_home = tmp_path / "profile" / ".grok"
    assert prepared.env["GROK_HOME"] == str(grok_home)
    assert prepared.env["HOME"] == str(tmp_path / "home"), "the Session home stays private"
    assert INFERENCE_KEY_ENV not in prepared.env and KEY not in str(prepared.env)
    assert prepared.secrets == [] and list(grok_home.iterdir()) == [], "no config.toml is written"
    argv = harness.start_turn(ctx, prepared).argv
    assert argv[argv.index("-m") + 1] == "grok-a"
    assert argv[argv.index("--effort") + 1] == "xhigh" and "--no-auto-update" in argv
    assert argv[argv.index("--output-format") + 1] == "streaming-messages-json"
    with pytest.raises(HarnessError) as err:
        harness.resume_turn(ctx, prepared, {"provider_id": "grok", "native_id": "s-1"})
    assert err.value.code == "context_unavailable"
    (grok_home / "sessions" / "cwd" / "s-1").mkdir(parents=True)
    resumed = harness.resume_turn(ctx, prepared, {"provider_id": "grok", "native_id": "s-1"}).argv
    assert resumed[-2:] == ["-r", "s-1"], "sessions are found on the Slot Volume"

    default = _grok_subscription(tmp_path)
    plain = harness.start_turn(default, harness.prepare(default, {})).argv
    assert "-m" not in plain and "--effort" not in plain, "the CLI's own default is used"

    missing = _grok_subscription(tmp_path)
    missing.inference["env"]["GROK_HOME"] = str(tmp_path / "not-mounted")
    with pytest.raises(HarnessError) as err:
        harness.prepare(missing, secrets())
    assert err.value.code == "connection_required"


def _agy(tmp_path: Path, **overrides) -> TurnContext:
    profile = tmp_path / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    ctx = context(tmp_path, "antigravity")
    ctx.inference = {
        "mode": "subscription",
        "provider_id": "antigravity",
        "env": {"HOME": str(profile), "AGY_CLI_DISABLE_AUTO_UPDATE": "1"},
    }
    for name, value in overrides.items():
        setattr(ctx, name, value)
    return ctx


def test_antigravity_turn_runs_print_mode_on_the_slot_profile(tmp_path) -> None:
    harness = HARNESSES["antigravity"]
    ctx = _agy(tmp_path, model="model-pro-1-high")
    prepared = harness.prepare(ctx, secrets())
    assert prepared.env["HOME"] == str(tmp_path / "profile"), "agy has no home override"
    assert prepared.env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"
    assert INFERENCE_KEY_ENV not in prepared.env and KEY not in str(prepared.env)
    assert "GEMINI_API_KEY" not in prepared.env and prepared.secrets == []
    assert list((tmp_path / "profile").iterdir()) == [], "nothing is written into the profile"
    invocation = harness.start_turn(ctx, prepared)
    argv = invocation.argv
    assert argv[argv.index("-p") + 1] == ctx.prompt and invocation.stdin is None
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--model") + 1] == "model-pro-1-high"
    assert "--effort" not in argv, "the effort is part of the model variant"
    assert invocation.cwd == ctx.worktree
    resumed = harness.resume_turn(
        ctx, prepared, {"provider_id": "antigravity", "native_id": "c-1"}
    ).argv
    assert resumed[-2:] == ["--conversation", "c-1"]
    assert harness.native_state_paths(prepared.home) == [], "no profile file is ever exported"
    assert harness.release(prepared) == []

    default = _agy(tmp_path)
    plain = harness.start_turn(default, harness.prepare(default, {})).argv
    assert "--model" not in plain and "--effort" not in plain


def test_antigravity_refuses_everything_but_a_mounted_slot(tmp_path) -> None:
    harness = HARNESSES["antigravity"]
    byok = context(tmp_path, "antigravity")
    with pytest.raises(HarnessError) as err:
        harness.prepare(byok, secrets())
    assert err.value.code == "connection_required", "no API-key mode exists"
    missing = _agy(tmp_path)
    missing.inference["env"]["HOME"] = str(tmp_path / "not-mounted")
    with pytest.raises(HarnessError) as err:
        harness.prepare(missing, {})
    assert err.value.code == "connection_required"
    # The signed-in CLI refuses ``--model <variant> --effort <level>``; so does the Harness.
    with pytest.raises(HarnessError) as err:
        harness.prepare(_agy(tmp_path, model="model-pro-1-high", effort="low"), {})
    assert err.value.code == "validation_failed"


def test_antigravity_recorded_real_stream_normalizes(tmp_path) -> None:
    harness = HARNESSES["antigravity"]
    out, state = replay("antigravity", "success", _agy(tmp_path))
    kinds = [o["type"] for o in out]
    assert kinds[0] == "execution.native_bound"
    assert kinds.count("tool.started") == 2 and kinds.count("tool.completed") == 2
    shell = next(
        o["payload"]
        for o in out
        if o["type"] == "tool.completed" and o["payload"]["name"] == "run_command"
    )
    assert shell["title"] == "cat hello.txt" and shell["output"] == "sbx\r\n"
    text = "".join(o["payload"]["content"] for o in out if o["type"].startswith("message.part"))
    assert text.endswith("printed:\n```\nsbx\n```\n")
    usage = harness.finish(state)[0]["payload"]
    assert usage["input_tokens"] == 35881 and usage["output_tokens"] == 168
    assert harness.classify_outcome({"exit_code": 0}, state).verdict == "success"


AGY_STREAM = [
    {"event": "init", "conversation_id": "c-1", "init": {"cwd": "/w", "tools": []}},
    {"event": "step_update", "step_update": {"step_index": 0, "step_type": "user_input"}},
    {
        "event": "step_update",
        "step_update": {
            "conversation_id": "c-1",
            "step_index": 1,
            "state": "ACTIVE",
            "step_type": "tool",
            "tool_name": "run_command",
            "tool_info": {"name": "run_command", "parameters": "{'CommandLine': 'echo hi'}"},
        },
    },
    {
        "event": "step_update",
        "step_update": {"step_index": 1, "state": "DONE", "step_type": "tool", "tool_info": {}},
    },
    {
        "event": "step_update",
        "step_update": {"step_index": 2, "step_type": "agent_response", "text_delta": "Hel"},
    },
    {
        "event": "step_update",
        "step_update": {"step_index": 2, "step_type": "agent_response", "text_delta": "lo"},
    },
    {
        "event": "result",
        "result": {
            "conversation_id": "c-1",
            "status": "SUCCESS",
            "response": "Hello",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 7,
                "thinking_tokens": 3,
                "cache_read_tokens": 60,
                "total_tokens": 107,
            },
        },
    },
]


def test_antigravity_stream_becomes_parts_tools_binding_and_real_usage(tmp_path) -> None:
    harness = HARNESSES["antigravity"]
    state = harness.new_state(_agy(tmp_path))
    out = [o for frame in AGY_STREAM for o in harness.normalize(json.dumps(frame), state)]
    kinds = [o["type"] for o in out]
    assert kinds == [
        "execution.native_bound",
        "tool.started",
        "tool.completed",
        "message.part_added",
        "message.part_updated",
    ]
    assert out[0]["payload"] == {"provider_id": "antigravity", "native_id": "c-1", "resumed": False}
    assert out[1]["payload"]["title"] == "echo hi" and out[1]["payload"]["name"] == "run_command"
    assert [o["payload"]["content"] for o in out[3:]] == ["Hel", "lo"]
    assert all(o["payload"]["mode"] == "append" for o in out[3:])
    usage = harness.finish(state)[0]["payload"]
    assert usage["input_tokens"] == 40 and usage["cached_input_tokens"] == 60
    assert usage["output_tokens"] == 7 and usage["reasoning_tokens"] == 3
    assert harness.classify_outcome({"exit_code": 0}, state).verdict == "success"
    assert harness.classify_outcome({"exit_code": None}, state).verdict == "unknown"
    assert harness.normalize("not json", state)[0]["payload"]["category"] == "malformed_frame"
    assert harness.normalize('{"event":"future_kind"}', state) == []


def test_antigravity_failures_are_classified_and_never_invent_usage(tmp_path) -> None:
    harness = HARNESSES["antigravity"]

    def outcome(error: str, stderr: str = "", code: int = 3):
        state = harness.new_state(_agy(tmp_path))
        frame = {"event": "result", "result": {"status": "ERROR", "error": error}}
        out = harness.normalize(json.dumps(frame), state)
        assert harness.finish(state) == [], "missing usage is absent, not synthetic zero"
        return out, harness.classify_outcome({"exit_code": code, "stderr_tail": stderr}, state)

    out, signed_out = outcome("Authentication expired. Please log back in.")
    assert out[0]["payload"]["credential_health"] == "invalid"
    assert (
        signed_out.error_code == "credential_invalid" and signed_out.credential_health == "invalid"
    )
    assert outcome("quota exceeded for this plan")[1].error_code == "rate_limited"
    conflict = "invalid model selection: --model m-high conflicts with --effort=low"
    assert outcome(conflict, code=1)[1].error_code == "provider_failed"
    silent = harness.new_state(_agy(tmp_path))
    hung = harness.classify_outcome(
        {"exit_code": 1, "stderr_tail": "authentication required"}, silent
    )
    assert hung.error_code == "credential_invalid", "print mode without a login"
    assert harness.classify_outcome({"exit_code": 0}, silent).verdict == "failure", "no result"

    resumed = _agy(tmp_path, native_binding={"provider_id": "antigravity", "native_id": "c-old"})
    state = harness.new_state(resumed)
    harness.normalize(json.dumps(AGY_STREAM[0]), state)
    assert harness.classify_outcome({"exit_code": 0}, state).error_code == "context_mismatch"


@pytest.mark.parametrize(
    "protocol,expected",
    [
        ("openai_chat", {"reasoningEffort": "none"}),
        ("anthropic_messages", {"thinking": {"type": "disabled"}}),
    ],
)
def test_opencode_passes_thinking_off_as_the_sdk_model_option(tmp_path, protocol, expected) -> None:
    harness = HARNESSES["opencode"]
    ctx = context(tmp_path, "opencode")
    ctx.inference = {"protocol": protocol, "base_url": URLS[protocol], "model": "vendor/model-1"}
    ctx.effort = "none"
    prepared = harness.prepare(ctx, secrets())
    config = json.loads(Path(prepared.env["OPENCODE_CONFIG"]).read_text())
    [model] = config["provider"]["sbx"]["models"].values()
    assert model["options"] == expected
    ctx.effort = None
    prepared = harness.prepare(ctx, secrets())
    config = json.loads(Path(prepared.env["OPENCODE_CONFIG"]).read_text())
    [model] = config["provider"]["sbx"]["models"].values()
    assert "options" not in model, "no reasoning parameter is sent unless one was chosen"


def test_codex_custom_api_config_never_carries_an_effort(tmp_path) -> None:
    harness = HARNESSES["codex"]
    ctx = context(tmp_path, "codex")
    ctx.effort = "none"
    prepared = harness.prepare(ctx, secrets())
    config = (Path(prepared.env["CODEX_HOME"]) / "config.toml").read_text()
    assert "model_reasoning_effort" not in config, "measured: not forwarded to a custom endpoint"

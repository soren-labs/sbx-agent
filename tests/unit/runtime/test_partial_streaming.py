"""True incremental streaming for the Messages-format CLIs (Claude Code, Grok Build).

The fixtures are streams recorded from the official CLIs with
``--include-partial-messages``: provider ``stream_event`` deltas interleaved with the
completed ``assistant`` frames for the same content blocks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from runtime.harnesses import messages_stream
from runtime.harnesses.protocol import TurnContext
from tests.unit.runtime.test_byok_harnesses import FIXTURES, HARNESSES, context, replay

PART_EVENTS = ("message.part_added", "message.part_updated")


def fold(observations: list[dict]) -> dict[str, dict]:
    """Apply part observations the way ingest and the Console do."""
    parts: dict[str, dict] = {}
    for o in observations:
        if o["type"] not in PART_EVENTS:
            continue
        p = o["payload"]
        part = parts.setdefault(p["part_key"], {"kind": p["kind"], "revision": 0, "content": ""})
        assert p["revision"] > part["revision"], "revisions only grow"
        assert (o["type"] == "message.part_added") == (part["revision"] == 0)
        part["revision"] = p["revision"]
        part["content"] = part["content"] + p["content"] if p["mode"] == "append" else p["content"]
    return parts


def completed_blocks(provider: str, name: str) -> tuple[list[str], list[str], list[dict]]:
    texts, thoughts, tools = [], [], []
    for line in (FIXTURES / provider / f"{name}.jsonl").read_text().splitlines():
        frame = json.loads(line)
        if frame.get("type") != "assistant":
            continue
        for block in frame["message"]["content"]:
            if block["type"] == "text" and block["text"]:
                texts.append(block["text"])
            elif block["type"] == "thinking" and block.get("thinking"):
                thoughts.append(block["thinking"])
            elif block["type"] == "tool_use":
                tools.append(block)
    return texts, thoughts, tools


@pytest.mark.parametrize("provider", ["claude", "grok"])
def test_argv_asks_the_cli_for_partial_messages(tmp_path: Path, provider: str) -> None:
    harness = HARNESSES[provider]
    ctx = context(tmp_path, provider)
    prepared = harness.prepare(ctx, {"inference": {"api_key": "k" * 24}})
    assert "--include-partial-messages" in harness.start_turn(ctx, prepared).argv


@pytest.mark.parametrize("provider", ["claude", "grok"])
def test_text_arrives_as_append_deltas_and_is_never_duplicated(
    tmp_path: Path, provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(messages_stream, "FLUSH_SECONDS", 0.0)  # one observation per delta
    observations, _ = replay(provider, "partial", context(tmp_path, provider))
    texts, thoughts, tools = completed_blocks(provider, "partial")
    parts = fold(observations)
    assert [p["content"] for p in parts.values() if p["kind"] == "text"] == texts
    assert [p["content"] for p in parts.values() if p["kind"] == "reasoning"] == thoughts
    appended = [o for o in observations if o["payload"].get("mode") == "append"]
    assert len(appended) > 5 * len(texts), "text grew incrementally, not block by block"
    # The completed assistant frame matched the streamed text: no replace was needed.
    assert not [o for o in observations if o["payload"].get("mode") == "replace"]
    # Stable keys in generation order.
    assert list(parts) == [f"part-{n}" for n in range(1, len(parts) + 1)]
    started = [o["payload"] for o in observations if o["type"] == "tool.started"]
    completed = [o["payload"] for o in observations if o["type"] == "tool.completed"]
    assert [t["tool_id"] for t in started] == [t["id"] for t in tools]
    assert [t["tool_id"] for t in completed] == [t["id"] for t in tools]
    # The input known only when the block completed reached the tool before its result.
    assert all(t["input"] for t in completed)


def test_coalescing_bounds_observations_without_losing_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter(x * 0.01 for x in range(1, 100000))  # 10 ms between deltas
    monkeypatch.setattr(messages_stream.time, "monotonic", lambda: next(clock))
    observations, _ = replay("claude", "partial", context(tmp_path, "claude"))
    texts, thoughts, _ = completed_blocks("claude", "partial")
    parts = fold(observations)
    assert [p["content"] for p in parts.values() if p["kind"] == "text"] == texts
    assert [p["content"] for p in parts.values() if p["kind"] == "reasoning"] == thoughts
    deltas = sum(
        '"content_block_delta"' in line
        for line in (FIXTURES / "claude" / "partial.jsonl").read_text().splitlines()
    )
    emitted = len([o for o in observations if o["type"] in PART_EVENTS])
    assert emitted < deltas / 3


def test_a_tool_is_announced_when_its_block_starts_and_named_from_streamed_input(
    tmp_path: Path,
) -> None:
    harness = HARNESSES["claude"]
    state = harness.new_state(context(tmp_path, "claude"))

    def event(**body: object) -> list[dict]:
        return harness.normalize(json.dumps({"type": "stream_event", "event": body}), state)

    event(type="message_start", message={"id": "m1"})
    block = {"type": "tool_use", "id": "call-1", "name": "Write", "input": {}}
    started = event(type="content_block_start", index=0, content_block=block)
    assert [o["type"] for o in started] == ["tool.started"]
    assert started[0]["payload"]["status"] == "running" and started[0]["payload"]["title"] == ""
    assert event(type="content_block_delta", index=0, delta={"partial_json": '{"file_pa'}) == []
    named = event(
        type="content_block_delta", index=0, delta={"partial_json": 'th": "src/a b.py", "cont'}
    )
    assert [(o["type"], o["payload"]["title"]) for o in named] == [("tool.updated", "src/a b.py")]
    assert event(type="content_block_delta", index=0, delta={"partial_json": 'ent": "x"}'}) == []
    full = {"file_path": "src/a b.py", "content": "x"}
    frame = {
        "type": "assistant",
        "message": {"id": "m1", "content": [{**block, "input": full}]},
    }
    filled = harness.normalize(json.dumps(frame), state)
    assert [(o["type"], o["payload"]["input"]) for o in filled] == [("tool.updated", full)]
    assert harness.normalize(json.dumps(frame), state) == [], "a repeated frame adds nothing"
    result = {"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}
    done = harness.normalize(json.dumps({"type": "user", "message": {"content": [result]}}), state)
    assert done[0]["type"] == "tool.completed" and done[0]["payload"]["input"] == full
    assert "partial" not in done[0]["payload"]


def stream(harness: object, state: dict, **body: object) -> list[dict]:
    return harness.normalize(json.dumps({"type": "stream_event", "event": body}), state)  # type: ignore[attr-defined]


def test_completed_block_reconciles_text_the_stream_got_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(messages_stream, "FLUSH_SECONDS", 0.0)
    harness = HARNESSES["claude"]
    state = harness.new_state(context(tmp_path, "claude"))
    out = stream(harness, state, type="message_start", message={"id": "m1"})
    out += stream(
        harness, state, type="content_block_start", index=0, content_block={"type": "text"}
    )
    for chunk in ("Hel", "lo"):
        delta = {"type": "text_delta", "text": chunk}
        out += stream(harness, state, type="content_block_delta", index=0, delta=delta)
    frame = {
        "type": "assistant",
        "message": {"id": "m1", "content": [{"type": "text", "text": "Hello!"}]},
    }
    out += harness.normalize(json.dumps(frame), state)
    out += stream(harness, state, type="content_block_stop", index=0)
    assert fold(out) == {"part-1": {"kind": "text", "revision": 3, "content": "Hello!"}}
    assert out[-1]["payload"]["mode"] == "replace"


def test_a_provider_retry_continues_the_cut_off_part_instead_of_repeating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(messages_stream, "FLUSH_SECONDS", 0.0)
    harness = HARNESSES["claude"]
    state = harness.new_state(context(tmp_path, "claude"))
    out = stream(harness, state, type="message_start", message={"id": "m1"})
    out += stream(
        harness, state, type="content_block_start", index=0, content_block={"type": "text"}
    )
    delta = {"type": "text_delta", "text": "The answer is"}
    out += stream(harness, state, type="content_block_delta", index=0, delta=delta)
    # The connection dropped: the CLI retries and the provider starts the message over.
    out += stream(harness, state, type="message_start", message={"id": "m2"})
    out += stream(
        harness, state, type="content_block_start", index=0, content_block={"type": "text"}
    )
    delta = {"type": "text_delta", "text": "The answer is 42."}
    out += stream(harness, state, type="content_block_delta", index=0, delta=delta)
    out += stream(harness, state, type="content_block_stop", index=0)
    assert fold(out) == {"part-1": {"kind": "text", "revision": 2, "content": "The answer is 42."}}


def test_sub_agent_streams_and_signature_only_thinking_add_no_parts(tmp_path: Path) -> None:
    harness = HARNESSES["claude"]
    state = harness.new_state(context(tmp_path, "claude"))
    nested = {
        "type": "stream_event",
        "parent_tool_use_id": "call-9",
        "event": {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": "x"},
        },
    }
    assert harness.normalize(json.dumps(nested), state) == []
    out = stream(harness, state, type="message_start", message={"id": "m1"})
    out += stream(
        harness, state, type="content_block_start", index=0, content_block={"type": "thinking"}
    )
    delta = {"type": "signature_delta", "signature": "REDACTED"}
    out += stream(harness, state, type="content_block_delta", index=0, delta=delta)
    frame = {
        "type": "assistant",
        "message": {"id": "m1", "content": [{"type": "thinking", "thinking": ""}]},
    }
    out += harness.normalize(json.dumps(frame), state)
    out += stream(harness, state, type="content_block_stop", index=0)
    assert out == [] and state["parts"] == 0


def test_streams_without_partial_events_still_add_whole_blocks_once(tmp_path: Path) -> None:
    ctx: TurnContext = context(tmp_path, "claude")
    observations, _ = replay("claude", "success", ctx)
    parts = [o["payload"] for o in observations if o["type"] in PART_EVENTS]
    assert parts and all(p["mode"] == "replace" and p["revision"] == 1 for p in parts)

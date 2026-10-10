"""Shared normalizer for CLIs that print Anthropic-Messages-shaped NDJSON.

Claude Code (``--output-format stream-json``) and Grok Build (``--output-format
streaming-messages-json``) emit the same envelope: a ``system``/``init`` frame with
the native ``session_id``, one ``assistant`` frame per completed content block group,
``user`` frames carrying ``tool_result`` blocks, and a final ``result`` frame with
usage and the error flag.

With ``--include-partial-messages`` Claude Code also relays the provider's own
``stream_event`` frames. Their ``text_delta``/``thinking_delta`` chunks become
``append`` revisions of one part as they arrive (coalesced to at most one observation
per ``FLUSH_SECONDS``), and a ``tool_use`` block is announced when it starts instead
of when its input is complete. The completed ``assistant`` frame for the same block
then only reconciles the part it already has: text is never added twice. Without
stream events (Grok Build, older CLIs) whole blocks arrive at once, exactly as before.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from runtime.harnesses.protocol import (
    HarnessOutcome,
    TurnContext,
    bounded,
    classify_text,
    obs,
)

# Provider deltas are a few characters each; one observation per delta would flood the
# journal. Text is forwarded at most this often and whenever a block ends.
FLUSH_SECONDS = 0.1
FLUSH_CHARS = 2000
PART_LIMIT = 64000
_TITLE_KEYS = ("command", "file_path", "path", "pattern", "description", "url")
_PARTIAL_TITLE = re.compile(  # a title key whose JSON string value has closed
    r'"(' + "|".join(_TITLE_KEYS) + r')"\s*:\s*("(?:[^"\\]|\\.)*")'
)


def new_state(context: TurnContext) -> dict[str, Any]:
    return {
        "expected": (context.native_binding or {}).get("native_id"),
        "native_id": None,
        "mismatch": False,
        "parts": 0,
        # Provider stream of the message being generated: its id, the content blocks by
        # index, and partial blocks a provider retry left behind (reused, not repeated).
        "stream": {"message_id": None, "blocks": {}, "orphans": []},
        "tools": {},
        "errors": [],
        "usage": None,
        "completed": False,
    }


def _result_text(content: Any) -> str:
    """Tool result content as text; Grok wraps its output in a JSON envelope."""
    if isinstance(content, list):
        content = "\n".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if not isinstance(content, str):
        return "" if content is None else json.dumps(content)
    if content.startswith("{"):
        try:
            envelope = json.loads(content)
        except ValueError:
            return content
        if isinstance(envelope, dict) and isinstance(envelope.get("output_for_prompt"), str):
            return envelope["output_for_prompt"]
    return content


def normalize(provider_id: str, line: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    line = line.strip()
    if not line:
        return []
    try:
        frame = json.loads(line)
    except ValueError:
        return [
            obs("diagnostic.reported", category="malformed_frame", message="unparseable CLI line")
        ]
    if not isinstance(frame, dict):
        return []
    out: list[dict[str, Any]] = []
    sid = frame.get("session_id")
    if isinstance(sid, str) and sid and state["native_id"] is None:
        state["native_id"] = sid
        if state["expected"] and sid != state["expected"]:
            state["mismatch"] = True
            out.append(
                obs(
                    "diagnostic.reported",
                    category="context_mismatch",
                    message="resumed CLI reported a different native session",
                )
            )
        out.append(
            obs(
                "execution.native_bound",
                provider_id=provider_id,
                native_id=sid,
                resumed=bool(state["expected"]),
            )
        )
    kind = frame.get("type")
    message = frame.get("message") if isinstance(frame.get("message"), dict) else {}
    blocks = message.get("content") if isinstance(message.get("content"), list) else []
    if kind == "stream_event":
        # Sub-agent streams belong to a tool call, not to the Turn's own reply.
        if not frame.get("parent_tool_use_id") and isinstance(frame.get("event"), dict):
            out.extend(_stream_event(frame["event"], state))
    elif kind == "assistant":
        stream = state["stream"]
        streamed = bool(message.get("id")) and message.get("id") == stream["message_id"]
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in ("text", "thinking"):
                text = str(block.get("text") or block.get("thinking") or "")
                live = _next_streamed(stream, block_type) if streamed else None
                if live is not None:
                    out.extend(_reconcile(live, text, state))
                    continue
                if not text:
                    continue  # signature-only thinking blocks carry nothing to show
                state["parts"] += 1
                out.append(
                    obs(
                        "message.part_added",
                        part_key=f"part-{state['parts']}",
                        kind="text" if block_type == "text" else "reasoning",
                        mode="replace",
                        revision=1,
                        content=bounded(text, PART_LIMIT),
                    )
                )
            elif block_type == "tool_use":
                call = str(block.get("id") or f"tool-{len(state['tools'])}")
                payload = {
                    "tool_id": call,
                    "name": str(block.get("name") or "tool"),
                    "status": "running",
                    "title": bounded(_title(block.get("input")), 300),
                    "input": bounded(block.get("input"), 2000),
                }
                started = state["tools"].get(call)
                if started is None:
                    state["tools"][call] = payload
                    out.append(obs("tool.started", **payload))
                elif started.pop("partial", False) and not started.get("done"):
                    # Announced from the stream before its input existed: fill it in.
                    started.update(payload)
                    out.append(obs("tool.updated", **payload))
    elif kind == "user":
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call = str(block.get("tool_use_id") or "")
            started = state["tools"].get(call)
            if not started or started.get("done"):
                continue
            started["done"] = True
            failed = bool(block.get("is_error"))
            out.append(
                obs(
                    "tool.completed",
                    **{k: v for k, v in started.items() if k not in ("done", "status", "partial")},
                    status="error" if failed else "completed",
                    output=bounded(_result_text(block.get("content")), 4000),
                    error=failed,
                )
            )
    elif kind == "system" and frame.get("subtype") == "api_retry":
        # Claude Code retries provider errors; a rejected key is reported the first time.
        status = frame.get("error_status")
        if status in (401, 403) and not state["errors"]:
            text = f"{frame.get('error') or 'authentication_failed'} ({status})"
            state["errors"].append(text)
            out.append(
                obs(
                    "diagnostic.reported",
                    category="provider_error",
                    message=text,
                    credential_health="invalid",
                )
            )
    elif kind == "result":
        state["completed"] = True
        usage = frame.get("usage") if isinstance(frame.get("usage"), dict) else None
        if usage:
            state["usage"] = {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cached_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
            }
        if frame.get("is_error") or frame.get("subtype") not in (None, "success"):
            listed = frame.get("errors") if isinstance(frame.get("errors"), list) else []
            text = " ".join(str(e) for e in listed) or str(
                frame.get("result") or frame.get("error") or frame.get("subtype") or ""
            )
            state["errors"].append(text)
            out.append(
                obs(
                    "diagnostic.reported",
                    category="provider_error",
                    message=bounded(text, 1000),
                    credential_health=classify_text(text),
                )
            )
    return out


def _stream_event(event: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    """One provider Messages stream event -> incremental part/tool observations."""
    stream = state["stream"]
    kind = event.get("type")
    index = event.get("index")
    out: list[dict[str, Any]] = []
    if kind == "message_start":
        # Blocks still open here were cut off (provider retry): the next block of the
        # same kind continues in the same part instead of showing the text twice.
        stream["orphans"] = [
            b
            for b in stream["blocks"].values()
            if not b["final"] and not b.get("stopped") and b.get("key")
        ]
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        stream["message_id"] = message.get("id")
        stream["blocks"] = {}
    elif kind == "content_block_start":
        block = event.get("content_block") if isinstance(event.get("content_block"), dict) else {}
        block_type = block.get("type")
        if block_type in ("text", "thinking"):
            live = {
                "type": block_type,
                "key": None,
                "revision": 0,
                "text": "",
                "pending": "",
                "sent_at": 0.0,
                "final": False,
                "reset": False,
            }
            for orphan in stream["orphans"]:
                if orphan["type"] == block_type:
                    stream["orphans"].remove(orphan)
                    live.update(key=orphan["key"], revision=orphan["revision"], reset=True)
                    break
            stream["blocks"][index] = live
            first = str(block.get("text") or block.get("thinking") or "")
            if first:
                out.extend(_grow(live, first, state))
        elif block_type == "tool_use" and block.get("id"):
            call = str(block["id"])
            stream["blocks"][index] = {"type": "tool_use", "call": call, "json": "", "final": True}
            if call not in state["tools"]:
                payload = {
                    "tool_id": call,
                    "name": str(block.get("name") or "tool"),
                    "status": "running",
                    "title": "",
                    "input": None,
                }
                state["tools"][call] = {**payload, "partial": True}
                out.append(obs("tool.started", **payload))
    elif kind == "content_block_delta":
        live = stream["blocks"].get(index)
        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
        if live is None:
            return out
        if live["type"] == "tool_use":
            out.extend(_tool_input(live, str(delta.get("partial_json") or ""), state))
        elif delta.get("type") in ("text_delta", "thinking_delta"):
            out.extend(_grow(live, str(delta.get("text") or delta.get("thinking") or ""), state))
    elif kind == "content_block_stop":
        live = stream["blocks"].get(index)
        if live is not None and live["type"] != "tool_use":
            live["stopped"] = True
            if not live["final"]:
                out.extend(_flush(live, state))
    return out


def _grow(live: dict[str, Any], text: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    if live["final"] or not text:
        return []
    live["pending"] += text
    due = time.monotonic() - live["sent_at"] >= FLUSH_SECONDS
    return _flush(live, state) if due or len(live["pending"]) >= FLUSH_CHARS else []


def _flush(live: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    chunk = live["pending"][: max(0, PART_LIMIT - len(live["text"]))]
    live["pending"] = ""
    if not chunk and not (live["reset"] and live["key"]):
        return []
    if live["key"] is None:
        state["parts"] += 1
        live["key"] = f"part-{state['parts']}"
    replace, live["reset"] = live["reset"], False
    live["text"] = chunk if replace else live["text"] + chunk
    live["revision"] += 1
    live["sent_at"] = time.monotonic()
    return [
        obs(
            "message.part_added" if live["revision"] == 1 else "message.part_updated",
            part_key=live["key"],
            kind="text" if live["type"] == "text" else "reasoning",
            mode="replace" if replace else "append",
            revision=live["revision"],
            content=chunk,
        )
    ]


def _next_streamed(stream: dict[str, Any], block_type: str) -> dict[str, Any] | None:
    for index in sorted(stream["blocks"], key=lambda i: (not isinstance(i, int), i)):
        live = stream["blocks"][index]
        if live["type"] == block_type and not live["final"]:
            return live
    return None


def _reconcile(live: dict[str, Any], text: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    """The completed block is authoritative: one replace revision only if it differs."""
    live["pending"] = ""
    if live["key"] is None:
        # Nothing visible was streamed (e.g. a signature-only thinking block).
        live["pending"] = text
        out = _flush(live, state)
        live["final"] = True
        return out
    live["final"] = True
    final = bounded(text, PART_LIMIT)
    if final == live["text"] and not live["reset"]:
        return []
    live["text"], live["reset"] = final, False
    live["revision"] += 1
    return [
        obs(
            "message.part_updated",
            part_key=live["key"],
            kind="text" if live["type"] == "text" else "reasoning",
            mode="replace",
            revision=live["revision"],
            content=final,
        )
    ]


def _tool_input(live: dict[str, Any], chunk: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Name the command or file as soon as the streamed input spells it out."""
    started = state["tools"].get(live["call"])
    if not chunk or started is None or not started.get("partial") or started.get("title"):
        return []
    if len(live["json"]) > 20000:
        return []  # a large body before any title key: wait for the completed block
    live["json"] += chunk
    match = _PARTIAL_TITLE.search(live["json"])
    if match is None:
        return []
    try:
        title = json.loads(match.group(2))
    except ValueError:
        return []
    if not isinstance(title, str) or not title.strip():
        return []
    started["title"] = bounded(title, 300)
    payload = {k: started[k] for k in ("tool_id", "name", "status", "title")}
    return [obs("tool.updated", **payload)]


def _title(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in _TITLE_KEYS:
        if isinstance(tool_input.get(key), str):
            return tool_input[key]
    return ""


def finish(source: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    if not state["usage"]:
        return []  # missing usage is absent, never synthetic zero
    return [obs("usage.observed", source=source, complete=True, **state["usage"])]


def classify(evidence: dict[str, Any], state: dict[str, Any]) -> HarnessOutcome:
    stderr = str(evidence.get("stderr_tail") or "")
    errors = " ".join(state["errors"])
    if state["mismatch"]:
        return HarnessOutcome(
            "failure", error_code="context_mismatch", message="native session mismatch"
        )
    if evidence.get("exit_code") is None:
        return HarnessOutcome(
            "unknown", "unknown", "outcome_unknown", "process stopped without exit status"
        )
    if evidence["exit_code"] == 0 and state["completed"] and not state["errors"]:
        return HarnessOutcome("success")
    lower = (errors + " " + stderr).lower()
    if "no conversation found" in lower or ("session" in lower and "not found" in lower):
        return HarnessOutcome(
            "failure", error_code="context_unavailable", message="native session not found"
        )
    health = classify_text(lower)
    if health == "invalid":
        return HarnessOutcome(
            "failure",
            "invalid",
            "credential_invalid",
            "provider rejected the credential",
            "replace_credential",
        )
    if health == "rate_limited":
        return HarnessOutcome(
            "failure", "rate_limited", "rate_limited", "provider rate limited", "retry_later"
        )
    return HarnessOutcome(
        "failure", "ok", "provider_failed", (errors or "CLI exited non-zero")[:500], "retry"
    )

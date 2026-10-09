"""Shared normalizer for CLIs that print Anthropic-Messages-shaped NDJSON.

Claude Code (``--output-format stream-json``) and Grok Build (``--output-format
streaming-messages-json``) emit the same envelope: a ``system``/``init`` frame with
the native ``session_id``, one ``assistant`` frame per completed content block group,
``user`` frames carrying ``tool_result`` blocks, and a final ``result`` frame with
usage and the error flag. Whole blocks arrive at once, so parts are added exactly once.
"""

from __future__ import annotations

import json
from typing import Any

from runtime.harnesses.protocol import (
    HarnessOutcome,
    TurnContext,
    bounded,
    classify_text,
    obs,
)


def new_state(context: TurnContext) -> dict[str, Any]:
    return {
        "expected": (context.native_binding or {}).get("native_id"),
        "native_id": None,
        "mismatch": False,
        "parts": 0,
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
    if kind == "assistant":
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in ("text", "thinking"):
                text = str(block.get("text") or block.get("thinking") or "")
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
                        content=bounded(text, 64000),
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
                if call not in state["tools"]:
                    state["tools"][call] = payload
                    out.append(obs("tool.started", **payload))
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
                    **{k: v for k, v in started.items() if k not in ("done", "status")},
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


def _title(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "description", "url"):
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

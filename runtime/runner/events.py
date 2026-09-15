"""JSONL event parsing, usage accumulation, and secret redaction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from runtime.runner.constants import USAGE_FIELDS

_SECRET_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "password",
    "secret",
    "authorization",
    "token",
    "openai_api_key",
    # grok streaming-json step-level usage events carry an account-bound
    # ``signature`` blob (GROK_SPIKE.md handoff): never let it reach
    # events.jsonl / events.raw.jsonl.
    "signature",
}

_SK_RE = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")


def empty_usage() -> dict[str, int]:
    return {key: 0 for key in USAGE_FIELDS}


def add_usage(acc: dict[str, int], usage: dict[str, Any]) -> None:
    for key in USAGE_FIELDS:
        if key not in usage or usage[key] is None:
            continue
        try:
            acc[key] = int(acc.get(key, 0)) + int(usage[key])
        except (TypeError, ValueError):
            continue


def redact_text(text: str) -> str:
    text = _SK_RE.sub("REDACTED", text)
    text = _BEARER_RE.sub("Bearer REDACTED", text)
    return text


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if str(key).lower() in _SECRET_KEYS:
                out[key] = "REDACTED"
            else:
                out[key] = redact_obj(value)
        return out
    if isinstance(obj, list):
        return [redact_obj(item) for item in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def redact_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return redact_text(stripped)
    redacted = redact_obj(obj)
    if redacted == obj and not isinstance(obj, str):
        return stripped
    return json.dumps(redacted, ensure_ascii=False)


def parse_event_line(line: str) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(object, is_bad_json)``. Empty lines are ignored, not bad."""
    stripped = line.strip()
    if not stripped:
        return None, False
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        return None, True
    if isinstance(obj, dict):
        return obj, False
    return None, False


@dataclass
class TurnState:
    thread_id: str | None = None
    last_message: str = ""
    usage: dict[str, int] = field(default_factory=empty_usage)
    bad_json_lines: int = 0

    def consume_line(self, line: str) -> tuple[dict[str, Any] | None, bool]:
        obj, bad = parse_event_line(line)
        if bad:
            self.bad_json_lines += 1
            return None, True
        if obj is None:
            return None, False
        self._apply(obj)
        return obj, False

    def consume_obj(self, obj: dict[str, Any]) -> None:
        """Apply one already-translated canonical event."""
        self._apply(obj)

    def _apply(self, obj: dict[str, Any]) -> None:
        event_type = obj.get("type")
        if event_type == "thread.started":
            thread_id = obj.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                self.thread_id = thread_id
        elif event_type == "turn.completed":
            usage = obj.get("usage")
            if isinstance(usage, dict):
                add_usage(self.usage, usage)
        item = obj.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text:
                self.last_message = text

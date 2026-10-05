"""Normalized observation envelope (RFC 167 §03/§04).

An Observation is provider-neutral evidence produced by a Harness
``normalize`` and recorded verbatim in the daemon spool. Observations are
data, not verdicts: the control plane reduces them into Session events and
projections. ``kind`` is a stable discriminator; ``native_*`` fields carry
provider lineage needed for resume and context-mismatch detection.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class ObservationKind(enum.StrEnum):
    THREAD_STARTED = "thread.started"  # first native context sighting
    TURN_STARTED = "turn.started"
    ITEM_STARTED = "item.started"
    ITEM_UPDATED = "item.updated"
    ITEM_COMPLETED = "item.completed"
    TOOL_OUTPUT = "tool.output"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    TURN_INTERRUPTED = "turn.interrupted"
    PROCESS_EXITED = "process.exited"  # supervisor terminal evidence
    DIAGNOSTIC = "diagnostic"  # provider/cli diagnostic line
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    NOOP = "noop"  # parseable but semantically empty frame


@dataclass(frozen=True)
class Usage:
    """Optional source-labelled usage. Missing fields stay ``None`` —
    never synthetic zero."""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    cost: float | None = None
    source: str = "provider"  # provider|cli|estimated

    def any(self) -> bool:
        return any(
            v is not None
            for v in (
                self.input_tokens,
                self.cached_input_tokens,
                self.cache_write_input_tokens,
                self.output_tokens,
                self.reasoning_output_tokens,
                self.cost,
            )
        )

    def to_dict(self) -> dict:
        out: dict = {"source": self.source}
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "cost",
        ):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    @staticmethod
    def from_dict(raw: dict) -> Usage:
        return Usage(
            input_tokens=raw.get("input_tokens"),
            cached_input_tokens=raw.get("cached_input_tokens"),
            cache_write_input_tokens=raw.get("cache_write_input_tokens"),
            output_tokens=raw.get("output_tokens"),
            reasoning_output_tokens=raw.get("reasoning_output_tokens"),
            cost=raw.get("cost"),
            source=str(raw.get("source", "provider")),
        )


@dataclass(frozen=True)
class Observation:
    """One normalized evidence item.

    ``item``/``message_part`` carry stable provider IDs when the CLI exposes
    them (OpenCode ``part.id``/``callID``); partial vs completed lives in the
    kind, never inferred downstream.
    """

    kind: ObservationKind
    native_session_id: str | None = None
    native_turn_id: str | None = None
    item: dict | None = None  # {id, type, status, ...} stable provider id
    text: str | None = None
    error: dict | None = None  # {code, message, retry_advice?}
    usage: Usage | None = None
    process: dict | None = None  # {pid?, exit_code, signal?, duration_ms}
    payload: dict = field(default_factory=dict)
    observed_at: float = 0.0  # daemon-local unix seconds

    def to_dict(self) -> dict:
        out: dict = {"kind": self.kind.value, "observed_at": self.observed_at}
        for key in ("native_session_id", "native_turn_id", "item", "text", "error", "process"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.usage is not None and self.usage.any():
            out["usage"] = self.usage.to_dict()
        if self.payload:
            out["payload"] = self.payload
        return out

    @staticmethod
    def from_dict(raw: dict) -> Observation:
        usage = raw.get("usage")
        return Observation(
            kind=ObservationKind(raw.get("kind", "noop")),
            native_session_id=raw.get("native_session_id"),
            native_turn_id=raw.get("native_turn_id"),
            item=raw.get("item"),
            text=raw.get("text"),
            error=raw.get("error"),
            usage=Usage.from_dict(usage) if isinstance(usage, dict) else None,
            process=raw.get("process"),
            payload=dict(raw.get("payload") or {}),
            observed_at=float(raw.get("observed_at") or 0.0),
        )

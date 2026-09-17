"""Structured run-error contract and normalization (SOR-82/A3).

One canonical failure shape for runs across every layer:

    {"code", "source", "message", "retryable", "retry_after"?}

``code`` is one of ``RUN_ERROR_CODES`` (mirrored in
``docs/contracts/api-v1.yaml`` ``x-canonical.run_error_codes``);
``source`` names the layer that produced the failure:

- ``provider``  — the provider CLI/API rejected or failed the request
- ``runtime``   — runner / sandbox infrastructure (timeout, spawn, lost record)
- ``control``   — control-plane decisions (cancel, agent closed mid-run)
- ``telemetry`` — event-stream/parse problems (``event_parse_error``)

``retryable`` means "a later attempt may succeed" (possibly on another
account/model); ``retry_after`` is the provider's hint in seconds when known.
SOR-63's scheduler consumes this contract for cooldown/failover decisions —
it must stay importable without FastAPI, so this module is stdlib-only.

Normalization rules:

- A turn that timed out maps to ``timeout``; a bad event stream maps to
  ``event_parse_error`` and is **never** silently downgraded — the run stays
  ``ERROR`` unless execution completeness is independently confirmed
  (SOR-82 acceptance gate).
- A non-zero provider CLI exit is classified from the recorded error text
  (``turns/<n>.json.error`` written by the runner, plus ``health``); the
  unclassifiable remainder falls back to ``runtime_error`` — never to a
  generic success.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

RunErrorCode = Literal[
    "auth_invalid",
    "rate_limited",
    "quota_exhausted",
    "model_unavailable",
    "model_capacity",
    "provider_unavailable",
    "runtime_error",
    "event_parse_error",
    "timeout",
    "cancelled",
]
RunErrorSource = Literal["provider", "runtime", "control", "telemetry"]

CODE_AUTH_INVALID: RunErrorCode = "auth_invalid"
CODE_RATE_LIMITED: RunErrorCode = "rate_limited"
CODE_QUOTA_EXHAUSTED: RunErrorCode = "quota_exhausted"
CODE_MODEL_UNAVAILABLE: RunErrorCode = "model_unavailable"
CODE_MODEL_CAPACITY: RunErrorCode = "model_capacity"
CODE_PROVIDER_UNAVAILABLE: RunErrorCode = "provider_unavailable"
CODE_RUNTIME_ERROR: RunErrorCode = "runtime_error"
CODE_EVENT_PARSE_ERROR: RunErrorCode = "event_parse_error"
CODE_TIMEOUT: RunErrorCode = "timeout"
CODE_CANCELLED: RunErrorCode = "cancelled"

SOURCE_PROVIDER: RunErrorSource = "provider"
SOURCE_RUNTIME: RunErrorSource = "runtime"
SOURCE_CONTROL: RunErrorSource = "control"
SOURCE_TELEMETRY: RunErrorSource = "telemetry"

RUN_ERROR_CODES: tuple[str, ...] = (
    CODE_AUTH_INVALID,
    CODE_RATE_LIMITED,
    CODE_QUOTA_EXHAUSTED,
    CODE_MODEL_UNAVAILABLE,
    CODE_MODEL_CAPACITY,
    CODE_PROVIDER_UNAVAILABLE,
    CODE_RUNTIME_ERROR,
    CODE_EVENT_PARSE_ERROR,
    CODE_TIMEOUT,
    CODE_CANCELLED,
)
RUN_ERROR_SOURCES: tuple[str, ...] = (
    SOURCE_PROVIDER,
    SOURCE_RUNTIME,
    SOURCE_CONTROL,
    SOURCE_TELEMETRY,
)

_MESSAGE_LIMIT = 500

# ``retry_after`` hints in provider text: "Retry-After: 45",
# "retry after 30 seconds", "try again in 12s".
_RETRY_AFTER_RES = (
    re.compile(r"retry[-_ ]?after[:= ]+(\d+(?:\.\d+)?)", re.I),
    re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*s", re.I),
)

# Ordered needle table: first match wins. Order matters — quota/billing
# phrases must beat generic throttling needles ("429 ... quota exceeded" is
# quota_exhausted, not rate_limited), and model-missing phrases must beat
# generic "unavailable" ("model unavailable" is model_unavailable).
_NEEDLES: tuple[tuple[tuple[str, ...], RunErrorCode, RunErrorSource, bool], ...] = (
    # Credential itself is bad; a bare retry cannot help.
    (
        (
            "401",
            "403",
            "unauthorized",
            "unauthenticated",
            "invalid api key",
            "incorrect api key",
            "expired api key",
            "invalid token",
            "expired token",
            "authentication failed",
            "authentication required",
            "not signed in",
            "not authenticated",
            "not logged in",
            "login required",
            "forbidden",
            "credential",
        ),
        "auth_invalid",
        "provider",
        False,
    ),
    # Provider/model is temporarily saturated; retryable backpressure.
    (
        (
            "overloaded",
            "at capacity",
            "model_capacity",
            "capacity exceeded",
            "too many concurrent",
            "concurrency limit",
            "concurrent sessions",
            "server is busy",
            "high demand",
            "currently busy",
        ),
        "model_capacity",
        "provider",
        True,
    ),
    # The named model does not exist on this account; retry needs a
    # different model, so the same request is not retryable.
    (
        (
            "model not found",
            "unknown model",
            "does not exist",
            "no such model",
            "invalid model",
            "unsupported model",
            "model_not_found",
            "model unavailable",
            "model_unavailable",
            "not a valid model",
            "model is not supported",
        ),
        "model_unavailable",
        "provider",
        False,
    ),
    # Account quota/billing exhausted; retryable later or on another account.
    (
        (
            "insufficient_quota",
            "insufficient quota",
            "quota exceeded",
            "quota exhausted",
            "quota_exceeded",
            "exceeded your current quota",
            "resource_exhausted",
            "resource exhausted",
            "billing",
            "credit balance",
            "spend limit",
            "payment required",
            "402",
        ),
        "quota_exhausted",
        "provider",
        True,
    ),
    # Request throttling; honor the provider's retry hint when present.
    (
        (
            "429",
            "rate_limit",
            "rate limit",
            "rate-limit",
            "too many requests",
            "throttl",
            "slow down",
            "quota",
            "exhausted",
        ),
        "rate_limited",
        "provider",
        True,
    ),
    # Provider endpoint unreachable / 5xx; retryable transport failure.
    (
        (
            "provider unavailable",
            "service unavailable",
            "temporarily unavailable",
            "bad gateway",
            "gateway timeout",
            "connection refused",
            "connection timed out",
            "request timed out",
            "econnrefused",
            "etimedout",
            "connection reset",
            "network error",
            "upstream",
            "502",
            "503",
            "504",
            "unavailable",
        ),
        "provider_unavailable",
        "provider",
        True,
    ),
    # The provider CLI could not even start — sandbox/image problem.
    (
        (
            "failed to start provider cli",
            "failed to start",
            "executable not found",
            "command not found",
        ),
        "runtime_error",
        "runtime",
        False,
    ),
)


# Secret-shaped fragments must never reach a public ``run.error`` message.
# Provider text is already redacted inside the runner (runtime.runner.events)
# before ``turns/<n>.json`` is written — this is the second seam, so a
# regression upstream still cannot echo token material to the API. Patterns
# mirror the runner redaction: sk-/Bearer plus the injected credential
# universe (sbx_ keys, OAuth JWTs, xAI/GitHub/AWS/Google/Linear tokens).
_SECRET_RES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "REDACTED"),
    (re.compile(r"(?i)bearer\s+\S+"), "Bearer REDACTED"),
    (re.compile(r"sbx_[0-9a-f]{16,}"), "REDACTED"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "REDACTED"),
    (re.compile(r"xai-[A-Za-z0-9_-]{16,}"), "REDACTED"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"), "REDACTED"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{16,}"), "REDACTED"),
    (re.compile(r"lin_(?:api|oauth)_[A-Za-z0-9]{16,}"), "REDACTED"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "REDACTED"),
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "REDACTED"),
)


def _clip(text: str) -> str:
    text = text.strip()
    for pattern, replacement in _SECRET_RES:
        text = pattern.sub(replacement, text)
    return text if len(text) <= _MESSAGE_LIMIT else text[: _MESSAGE_LIMIT - 1] + "…"


def clip_message(text: str) -> str:
    """``_clip`` for callers outside this module (e.g. the ``sbx`` CLI).

    The same second-seam guarantee applies to any user-facing render of a
    run error: secret-shaped fragments are redacted and the text is bounded.
    """
    return _clip(text)


def _retry_after(text: str) -> float | None:
    for pattern in _RETRY_AFTER_RES:
        match = pattern.search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


@dataclass(frozen=True)
class RunError:
    """One canonical structured run failure.

    Serialized as ``run.error`` in ``/v1`` responses (``api-v1.yaml``
    ``RunError`` schema). ``from_dict`` is strict — both ``code`` and
    ``source`` must be canonical — so a malformed stored record surfaces as
    "no error" rather than a half-parsed one.
    """

    code: str
    source: str
    message: str
    retryable: bool = False
    retry_after: float | None = None

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "source": self.source,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> RunError | None:
        if not isinstance(raw, Mapping):
            return None
        code = raw.get("code")
        source = raw.get("source")
        if code not in RUN_ERROR_CODES or source not in RUN_ERROR_SOURCES:
            return None
        message = raw.get("message")
        retry_after = raw.get("retry_after")
        try:
            retry_after_value = float(retry_after) if retry_after is not None else None
        except (TypeError, ValueError):
            retry_after_value = None
        return cls(
            code=str(code),
            source=str(source),
            message=str(message) if isinstance(message, str) else str(code),
            retryable=bool(raw.get("retryable")),
            retry_after=retry_after_value,
        )


def classify_failure(text: str | None) -> RunError | None:
    """Map provider-style error text to a canonical ``RunError``.

    Needle match is case-insensitive substring over the whole text; the first
    table row wins. ``None`` means "no recognisable signature" — callers fall
    back to ``runtime_error``.
    """
    if not text:
        return None
    haystack = text.lower()
    for needles, code, source, retryable in _NEEDLES:
        if any(needle in haystack for needle in needles):
            return RunError(
                code=code,
                source=source,
                message=_clip(text),
                retryable=retryable,
                retry_after=_retry_after(text),
            )
    return None


def run_error_from_turn(payload: Mapping[str, Any] | None) -> RunError | None:
    """Normalize a ``turns/<n>.json`` payload into a ``RunError``.

    ``None`` means the turn recorded a clean success. A missing or
    uninformative record still yields ``runtime_error`` — failure detail must
    never collapse into an unmarked "ok".
    """
    if payload is None:
        return RunError(
            "runtime_error",
            "runtime",
            "run failed; turn record unavailable",
            retryable=True,
        )
    status = str(payload.get("status") or "")
    health = str(payload.get("health") or "")
    exit_code = payload.get("exit_code")
    raw_detail = payload.get("error")
    detail = raw_detail if isinstance(raw_detail, str) else ""
    try:
        bad_lines = int(payload.get("bad_json_lines") or 0)
    except (TypeError, ValueError):
        bad_lines = 0

    if status == "timeout":
        return RunError(
            "timeout",
            "runtime",
            _clip(detail or "turn exceeded max seconds"),
            retryable=True,
        )
    if status == "bad_json" or bad_lines > 0:
        # Parse corruption is a telemetry failure: it stays an error even
        # when a turn.completed arrived, because execution completeness was
        # not independently confirmed (SOR-82).
        count = bad_lines if bad_lines > 0 else "some"
        return RunError(
            "event_parse_error",
            "telemetry",
            _clip(detail or f"{count} unparseable line(s) in provider event stream"),
        )
    if status == "auth_invalid" or health == "auth_invalid":
        return RunError(
            "auth_invalid",
            "provider",
            _clip(detail or "provider authentication failed"),
        )
    if status == "cancelled":
        return RunError(
            "cancelled",
            "control",
            _clip(detail or "run cancelled by caller"),
        )
    if status == "internal":
        # Runner-internal failure (spawn/IO before the provider ran). The
        # hint may still classify (e.g. "failed to start provider CLI").
        classified = classify_failure(detail)
        if classified is not None:
            return classified
        return RunError(
            "runtime_error",
            "runtime",
            _clip(detail or "runner failed before the turn completed"),
        )
    if status == "success" and not detail:
        return None

    # Provider CLI failed (or a provider error event was recorded): classify
    # the recorded text, then the adapter health hint, then give up honestly.
    text = detail or str(payload.get("message") or "")
    classified = classify_failure(text)
    if classified is not None:
        return classified
    if health == "rate_limited":
        return RunError(
            "rate_limited",
            "provider",
            _clip(detail or "provider rate limited the request"),
            retryable=True,
        )
    if status == "success":
        # A provider error event fired but the turn still completed; nothing
        # to report (the error was non-fatal to execution).
        return None
    message = detail or f"provider CLI exited with code {exit_code}"
    return RunError("runtime_error", "provider", _clip(message))


def run_error_for_run(
    run_status: str,
    *,
    payload: Mapping[str, Any] | None = None,
    cancelled: bool = False,
    agent_status: str | None = None,
) -> RunError | None:
    """Structured ``error`` for a Cursor-shaped run status.

    ``payload`` is the ``turns/<n>.json`` record when readable;
    ``cancelled`` marks an explicit ``/v1`` cancel; ``agent_status`` is the
    session record status used to tell an abandoned sandbox (``lost``) from
    an idle timeout (``timed_out``).
    """
    if run_status in ("", "CREATING", "RUNNING"):
        return None
    if run_status == "FINISHED":
        # Completeness was independently confirmed by the turn record, so a
        # recorded parse failure is downgraded — explicitly — to a warning
        # carried on the run. Non-fatal provider noise stays quiet.
        if payload is not None:
            warning = run_error_from_turn(payload)
            if warning is not None and warning.code == CODE_EVENT_PARSE_ERROR:
                return warning
        return None
    if run_status == "CANCELLED":
        message = (
            "run cancelled by caller" if cancelled else "agent closed before the run completed"
        )
        return RunError("cancelled", "control", message)
    if run_status == "EXPIRED":
        if payload is not None:
            expired = run_error_from_turn(payload)
            if expired is not None:
                return expired
        if agent_status == "lost":
            return RunError(
                "runtime_error",
                "runtime",
                "sandbox was lost before the run completed",
                retryable=True,
            )
        return RunError(
            "timeout",
            "runtime",
            "agent timed out before the run completed",
            retryable=True,
        )
    # ERROR (or any unrecognised terminal status): the turn record decides.
    error = run_error_from_turn(payload)
    if error is None:
        # Contradictory record: status says failed, payload claims success.
        error = RunError(
            "runtime_error",
            "runtime",
            "run failed without a recorded cause",
        )
    return error

"""Unit tests for the structured run-error contract (SOR-82/A3).

Covers the canonical classifier against realistic provider error shapes
(codex/antigravity/grok/devin), turn-payload normalization, and the
unknown-error fallback.
"""

from __future__ import annotations

import pytest
from control.run_errors import (
    CODE_AUTH_INVALID,
    CODE_CANCELLED,
    CODE_EVENT_PARSE_ERROR,
    CODE_MODEL_CAPACITY,
    CODE_MODEL_UNAVAILABLE,
    CODE_PROVIDER_UNAVAILABLE,
    CODE_QUOTA_EXHAUSTED,
    CODE_RATE_LIMITED,
    CODE_RUNTIME_ERROR,
    CODE_TIMEOUT,
    SOURCE_CONTROL,
    SOURCE_PROVIDER,
    SOURCE_RUNTIME,
    SOURCE_TELEMETRY,
    classify_failure,
    run_error_for_run,
    run_error_from_turn,
)


def _turn(**over):
    payload = {
        "status": "codex_error",
        "exit_code": 2,
        "health": "ok",
        "error": None,
        "bad_json_lines": 0,
    }
    payload.update(over)
    return payload


# ---------------------------------------------------------------------------
# classify_failure: provider-like messages for the four harness providers.
# Shapes come from the provider adapters / fixtures: codex flat error +
# turn.failed; antigravity/grok/devin {error: {message}} + item errors.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "code", "source", "retryable"),
    [
        # auth — codex: {"type":"error","message":"401 Unauthorized: ..."};
        # devin: {"type":"error","error":{"code":"not_authenticated","message":"..."}};
        # grok/antigravity: 401/403 health strings.
        ("401 Unauthorized: invalid or expired token", CODE_AUTH_INVALID, SOURCE_PROVIDER, False),
        ("403 Forbidden", CODE_AUTH_INVALID, SOURCE_PROVIDER, False),
        ("not_authenticated: login required", CODE_AUTH_INVALID, SOURCE_PROVIDER, False),
        ("Not signed in.", CODE_AUTH_INVALID, SOURCE_PROVIDER, False),
        ("account credentials invalid or revoked", CODE_AUTH_INVALID, SOURCE_PROVIDER, False),
        ("Invalid API key, Please generate a new key", CODE_AUTH_INVALID, SOURCE_PROVIDER, False),
        ("unauthorized", CODE_AUTH_INVALID, SOURCE_PROVIDER, False),
        # rate limiting — all providers emit 429 / rate-limit shapes on the
        # top-level error event or in health strings.
        ("429 rate limit", CODE_RATE_LIMITED, SOURCE_PROVIDER, True),
        ("rate_limit_exceeded", CODE_RATE_LIMITED, SOURCE_PROVIDER, True),
        ("rate limit reached", CODE_RATE_LIMITED, SOURCE_PROVIDER, True),
        ("429 Too Many Requests", CODE_RATE_LIMITED, SOURCE_PROVIDER, True),
        ("exhausted", CODE_RATE_LIMITED, SOURCE_PROVIDER, True),
        # quota exhaustion.
        ("quota exceeded", CODE_QUOTA_EXHAUSTED, SOURCE_PROVIDER, True),
        ("quota exhausted", CODE_QUOTA_EXHAUSTED, SOURCE_PROVIDER, True),
        # model routing — antigravity 404s surface model-shaped messages;
        # adapter health strings carry the same phrasing.
        ("model not found", CODE_MODEL_UNAVAILABLE, SOURCE_PROVIDER, False),
        ("model unavailable", CODE_MODEL_UNAVAILABLE, SOURCE_PROVIDER, False),
        # capacity / availability backpressure.
        ("server overloaded", CODE_MODEL_CAPACITY, SOURCE_PROVIDER, True),
        ("capacity exceeded", CODE_MODEL_CAPACITY, SOURCE_PROVIDER, True),
        ("service unavailable", CODE_PROVIDER_UNAVAILABLE, SOURCE_PROVIDER, True),
        ("502 bad gateway", CODE_PROVIDER_UNAVAILABLE, SOURCE_PROVIDER, True),
    ],
)
def test_classify_failure_provider_needles(message, code, source, retryable):
    err = classify_failure(message)
    assert err is not None
    assert err.code == code
    assert err.source == source
    assert err.retryable is retryable


def test_classify_failure_spawn_hint_is_runtime():
    err = classify_failure("failed to start provider CLI: No such file or directory")
    assert err is not None
    assert err.code == CODE_RUNTIME_ERROR
    assert err.source == SOURCE_RUNTIME


def test_classify_failure_unknown_returns_none():
    # Unrecognised text is honest "no signature" — callers map the
    # remainder to runtime_error, never silently drop it.
    assert classify_failure("something entirely unexpected happened") is None
    assert classify_failure("") is None
    assert classify_failure(None) is None


def test_classify_failure_retry_after():
    err = classify_failure("429 rate limit; retry after 30 seconds")
    assert err is not None
    assert err.retry_after == 30.0
    assert err.public()["retry_after"] == 30.0
    no_hint = classify_failure("429 rate limit")
    assert no_hint is not None
    assert "retry_after" not in no_hint.public()


def test_error_message_never_carries_secret_fragments():
    """SOR-101: ``run.error.message`` is a second redaction seam — provider
    text that slipped a token past the runner redaction still cannot reach
    the API."""
    err = classify_failure(
        "401 unauthorized: bearer sbx_0123456789abcdef0123 rejected; "
        "api key sk-THISLEAKEDVALUE12; "
        "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3In0.signaturepart"
    )
    assert err is not None and err.code == CODE_AUTH_INVALID
    for fragment in (
        "sbx_0123456789abcdef0123",
        "sk-THISLEAKEDVALUE12",
        "eyJhbGciOiJIUzI1NiJ9",
    ):
        assert fragment not in err.message
    assert "REDACTED" in err.message
    # The same scrub applies on the turn-record path (timeout/cancel/detail).
    timed_out = run_error_from_turn(
        _turn(status="timeout", error="turn died with key sk-THISLEAKEDVALUE12 visible")
    )
    assert timed_out is not None
    assert "sk-THISLEAKEDVALUE12" not in timed_out.message


# ---------------------------------------------------------------------------
# run_error_from_turn: payload normalization.
# ---------------------------------------------------------------------------


def test_turn_timeout_is_expired():
    err = run_error_from_turn(_turn(status="timeout", exit_code=3, health="timeout"))
    assert err.code == CODE_TIMEOUT
    assert err.source == SOURCE_RUNTIME
    assert err.retryable is True


def test_turn_auth_invalid_exit():
    err = run_error_from_turn(_turn(status="auth_invalid", exit_code=5, health="auth_invalid"))
    assert err.code == CODE_AUTH_INVALID
    assert err.source == SOURCE_PROVIDER


def test_turn_bad_json_never_silent():
    err = run_error_from_turn(_turn(status="bad_json", exit_code=4, bad_json_lines=2))
    assert err is not None
    assert err.code == CODE_EVENT_PARSE_ERROR
    assert err.source == SOURCE_TELEMETRY
    assert "unparseable" in err.message


def test_turn_bad_json_with_runner_hint():
    err = run_error_from_turn(
        _turn(status="bad_json", exit_code=4, error="bad json in event stream", bad_json_lines=1)
    )
    assert err is not None
    assert err.code == CODE_EVENT_PARSE_ERROR
    assert "bad json" in err.message


def test_turn_provider_message_from_error_field():
    err = run_error_from_turn(
        _turn(status="codex_error", error="401 Unauthorized: invalid or expired token")
    )
    assert err.code == CODE_AUTH_INVALID
    assert err.source == SOURCE_PROVIDER
    assert "invalid or expired token" in err.message


def test_turn_provider_message_fallback_to_message_field():
    # Older turn payloads may not have `error`; the message field is the
    # next-best provider diagnosis.
    payload = _turn(status="codex_error")
    del payload["error"]
    payload["message"] = "429 rate limit"
    err = run_error_from_turn(payload)
    assert err.code == CODE_RATE_LIMITED
    assert err.retryable is True


def test_turn_health_fallback_when_no_message():
    err = run_error_from_turn(_turn(status="codex_error", health="auth_invalid"))
    assert err is not None
    assert err.code == CODE_AUTH_INVALID
    err = run_error_from_turn(_turn(status="codex_error", health="rate_limited"))
    assert err is not None
    assert err.code == CODE_RATE_LIMITED


def test_turn_internal_unknown_fallback():
    err = run_error_from_turn(_turn(status="internal"))
    assert err is not None
    assert err.code == CODE_RUNTIME_ERROR
    assert err.source == SOURCE_RUNTIME
    assert "runner" in err.message


def test_turn_cancelled():
    err = run_error_from_turn(_turn(status="cancelled", exit_code=6))
    assert err is not None
    assert err.code == CODE_CANCELLED
    assert err.source == SOURCE_CONTROL


def test_turn_success_is_no_error():
    assert run_error_from_turn(_turn(status="success", exit_code=0)) is None


# ---------------------------------------------------------------------------
# run_error_for_run: run-level status synthesis.
# ---------------------------------------------------------------------------


def test_run_error_none_on_success_and_running():
    assert run_error_for_run("FINISHED") is None
    assert run_error_for_run("RUNNING") is None
    assert run_error_for_run("CREATING") is None


def test_run_error_cancelled():
    err = run_error_for_run("CANCELLED", cancelled=True)
    assert err.code == CODE_CANCELLED
    assert err.source == SOURCE_CONTROL
    # Operator cancellation isn't retried automatically.
    assert err.retryable is False


def test_run_error_expired_without_payload():
    err = run_error_for_run("EXPIRED")
    assert err.code == CODE_TIMEOUT
    assert err.source == SOURCE_RUNTIME


def test_run_error_uses_turn_payload():
    err = run_error_for_run(
        "ERROR",
        payload=_turn(status="codex_error", error="quota exceeded"),
    )
    assert err.code == CODE_QUOTA_EXHAUSTED


def test_run_error_missing_turn_record_is_diagnosable():
    err = run_error_for_run("ERROR")
    assert err is not None
    assert err.code == CODE_RUNTIME_ERROR
    assert err.source == SOURCE_RUNTIME
    assert "turn record unavailable" in err.message


def test_run_error_generic():
    err = run_error_for_run("ERROR", payload=_turn(status="codex_error", error="huh"))
    assert err is not None
    assert err.code == CODE_RUNTIME_ERROR
    assert err.source == SOURCE_PROVIDER

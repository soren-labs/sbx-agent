"""Unit tests for ``control.latency.observe`` (SOR-118)."""

from __future__ import annotations

import json
import logging

import pytest
from control.latency import observe


def _payloads(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [
        json.loads(record.getMessage()) for record in caplog.records if record.name == "sbx.latency"
    ]


def test_observe_emits_one_json_line(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="sbx.latency"):
        with observe("test.op", agent_id="a1", members=3):
            pass

    payloads = _payloads(caplog)
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["event"] == "sbx.latency"
    assert payload["op"] == "test.op"
    assert payload["outcome"] == "ok"
    assert payload["agent_id"] == "a1"
    assert payload["members"] == 3
    assert payload["ms"] >= 0


def test_observe_marks_error_and_reraises(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="sbx.latency"):
        with pytest.raises(ValueError, match="boom"):
            with observe("test.op"):
                raise ValueError("boom")

    payload = _payloads(caplog)[-1]
    assert payload["outcome"] == "error"
    assert payload["op"] == "test.op"


def test_observe_bounds_fields_to_scalars(caplog: pytest.LogCaptureFixture) -> None:
    """A stray object field can never dump state into the log line."""
    with caplog.at_level(logging.INFO, logger="sbx.latency"):
        with observe("test.op", blob="x" * 500, nested={"a": "b"}):
            pass

    payload = _payloads(caplog)[-1]
    assert len(payload["blob"]) <= 201  # truncated, not the full string
    assert payload["nested"] == "{'a': 'b'}"  # str(), never a nested dump


def test_observe_survives_a_logging_failure(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Instrumentation is best-effort: a broken sink cannot break the path."""
    import control.latency as latency

    def broken(_msg: str) -> None:
        raise RuntimeError("sink down")

    monkeypatch.setattr(latency._log, "info", broken)
    with observe("test.op"):  # must not raise
        pass

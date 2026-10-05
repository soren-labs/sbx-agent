"""Job worker backoff policy — pure unit (no DB)."""

from __future__ import annotations

from control.jobs.worker import backoff_seconds


def test_exponential_growth():
    assert backoff_seconds(1) == 0.5
    assert backoff_seconds(2) == 1.0
    assert backoff_seconds(3) == 2.0
    assert backoff_seconds(4) == 4.0


def test_capped():
    assert backoff_seconds(20) == 60.0
    assert backoff_seconds(8) <= 60.0


def test_monotonic():
    seq = [backoff_seconds(i) for i in range(1, 10)]
    assert seq == sorted(seq)

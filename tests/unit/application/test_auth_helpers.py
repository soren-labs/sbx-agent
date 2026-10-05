"""Auth pure-function unit tests: token hashing, rate limiting, email
normalization (RFC 167 §06 bounded flows)."""

import pytest
from control.application.auth import (
    RateLimiter,
    _hash_token,
)
from control.domain.errors import DomainError
from control.domain.identity import normalize_email

pytestmark = pytest.mark.unit


class TestTokenHash:
    def test_deterministic_prefixed(self):
        h = _hash_token("abc")
        assert h.startswith("sha256:")
        assert _hash_token("abc") == h
        assert _hash_token("abd") != h
        assert "abc" not in h


class TestRateLimiter:
    def test_bounded(self):
        rl = RateLimiter(limit=3, window_s=60)
        for _ in range(3):
            rl.check("k")
        with pytest.raises(DomainError) as ei:
            rl.check("k")
        assert ei.value.code == "rate_limited"

    def test_independent_keys(self):
        rl = RateLimiter(limit=1, window_s=60)
        rl.check("a")
        rl.check("b")  # no raise
        with pytest.raises(DomainError):
            rl.check("a")


class TestEmailNormalize:
    def test_case_and_space(self):
        assert normalize_email("  User@Example.COM ") == "user@example.com"

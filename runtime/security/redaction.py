"""Known-secret and pattern redaction before spool/trace/error emission."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REDACTED = "REDACTED"
_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\bak-[A-Za-z0-9]{12,}"),
    re.compile(r"\bas-[A-Za-z0-9]{12,}"),
)
_SECRET_KEY = re.compile(
    r"(^|_|-)(token|secret|password|api[_-]?key|apikey|authorization|credential)($|_|-)", re.I
)
_SAFE = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_input_tokens",
        "credential_health",
    }
)


class Redactor:
    def __init__(self, known: Iterable[str] = ()) -> None:
        self.known = sorted({k for k in known if k and len(k) >= 6}, key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self.known:
            value = value.replace(secret, REDACTED)
        for pattern in _PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {
                k: (
                    REDACTED
                    if k not in _SAFE and _SECRET_KEY.search(str(k)) and isinstance(v, str) and v
                    else self.value(v)
                )
                for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.value(v) for v in value]
        return value

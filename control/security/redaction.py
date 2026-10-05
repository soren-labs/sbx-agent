"""Known-secret redaction and structured secret-field guards (RFC 06).

Substring replacement alone is insufficient: structured payloads are also
filtered by key name before anything reaches the journal, spool or logs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REDACTED = "REDACTED"

_SECRET_KEYS = re.compile(
    r"(^|_|-)(token|secret|password|passwd|api[_-]?key|apikey|authorization|"
    r"cookie|credential|private[_-]?key|access[_-]?key|refresh)($|_|-)",
    re.IGNORECASE,
)
# Fields whose names look secret but carry only safe identifiers.
_SAFE_KEYS = frozenset(
    {
        "token_id_fingerprint",
        "credential_version_id",
        "credential_kind",
        "credential_health",
        "credential_lease_ref",
        "token_count",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "tokens",
    }
)
_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"\bak-[A-Za-z0-9]{12,}"),
    re.compile(r"\bas-[A-Za-z0-9]{12,}"),
    re.compile(r"sbx_[a-z]+_[A-Za-z0-9_-]{24,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def is_secret_key(name: str) -> bool:
    return name not in _SAFE_KEYS and bool(_SECRET_KEYS.search(name))


def redact_text(text: str, known: Iterable[str] = ()) -> str:
    if not text:
        return text
    for secret in sorted({s for s in known if s and len(s) >= 6}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact(value: Any, known: Iterable[str] = ()) -> Any:
    known = tuple(known)
    if isinstance(value, dict):
        return {
            k: (REDACTED if is_secret_key(str(k)) and v not in (None, "") else redact(v, known))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v, known) for v in value]
    if isinstance(value, str):
        return redact_text(value, known)
    return value


def find_secret_fields(value: Any, path: str = "") -> list[str]:
    """Paths of secret-looking keys carrying non-redacted values."""
    found: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{path}.{k}" if path else str(k)
            if is_secret_key(str(k)) and v not in (None, "", REDACTED):
                found.append(here)
            found.extend(find_secret_fields(v, here))
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            found.extend(find_secret_fields(v, f"{path}[{i}]"))
    return found

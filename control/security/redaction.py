"""Known-secret redaction and structured secret-field guards (RFC 06).

Substring replacement alone is insufficient: structured payloads are also
filtered by key name before anything reaches the journal, spool or logs.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import traceback
from collections import OrderedDict
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


class _KnownSecrets:
    """Process-local, bounded registry of plaintext credential values this control-plane
    process has decrypted or sealed, so logs and surfaced faults can redact them even
    when they arrive embedded in arbitrary exception text. Memory only."""

    def __init__(self, limit: int = 4096) -> None:
        self.limit = limit
        self._values: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()
        self._regex: re.Pattern[str] | None = None

    def add(self, value: Any) -> None:
        if isinstance(value, dict):
            for v in value.values():
                self.add(v)
        elif isinstance(value, list | tuple):
            for v in value:
                self.add(v)
        elif isinstance(value, str) and len(value) >= 8:
            with self._lock:
                if value not in self._values:
                    self._regex = None
                self._values[value] = None
                self._values.move_to_end(value)
                while len(self._values) > self.limit:
                    self._values.popitem(last=False)
            # Native auth_json arrives as a string; exception text may contain
            # an individual token rather than that entire uploaded document.
            try:
                parsed = json.loads(value)
            except ValueError:
                return
            if isinstance(parsed, dict | list):
                self.add(parsed)

    def values(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._values)

    def redact(self, text: str) -> str:
        with self._lock:
            if self._regex is None and self._values:
                ordered = sorted(self._values, key=len, reverse=True)
                self._regex = re.compile("|".join(map(re.escape, ordered)))
            regex = self._regex
        return regex.sub(REDACTED, text) if regex is not None else text


KNOWN_SECRETS = _KnownSecrets()


def scrub(text: str) -> str:
    """Redact registered credential values and secret patterns from free text."""
    return redact_text(KNOWN_SECRETS.redact(text)) if text else text


def scrub_value(value: Any) -> Any:
    """``scrub`` every string inside a JSON-like value (keys are left untouched)."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: scrub_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub_value(v) for v in value]
    return value


def safe_traceback(exc: BaseException) -> str:
    return scrub("".join(traceback.format_exception(exc)))


class RedactingFilter(logging.Filter):
    """Handler filter: renders message and traceback, then redacts both."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = scrub("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        return True


def install_log_redaction(*loggers: logging.Logger) -> None:
    """Attach the redacting filter to every handler of the given loggers (root default)."""
    for logger in loggers or (logging.getLogger(),):
        for handler in logger.handlers:
            if not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter())

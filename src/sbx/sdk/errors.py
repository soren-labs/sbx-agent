"""SDK errors mirroring the API error shape."""

from __future__ import annotations

from typing import Any


class SBXError(Exception):
    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        status: int | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        request_id: str | None = None,
        action: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}
        self.retryable = retryable
        self.request_id = request_id
        self.action = action


class OutcomeUnknown(SBXError):
    """The Turn's outcome could not be proven; acknowledge before new work."""


class DeadlineExceeded(SBXError):
    pass

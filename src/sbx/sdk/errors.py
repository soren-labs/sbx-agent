"""SDK error types for the sbx-browser public ``/v1`` API (SOR-226).

``SbxApiError`` decodes the canonical error body
``{error: {code, message, retryable, action, retry_after?, details?}}``;
``SbxTransportError`` marks bounded HTTP calls that failed at the transport
layer and names the durable read to run before retrying.
"""

from __future__ import annotations

from typing import Any

import httpx


class SbxApiError(RuntimeError):
    """Canonical ``{error:{...}}`` body as an exception.

    ``code`` is one of the catalog ``error_subcodes``; ``retryable`` /
    ``action`` are the catalog hints; ``retry_after`` / ``details`` are
    optional as in the contract.
    """

    def __init__(
        self,
        status: int,
        code: str | None,
        message: str | None,
        retry_after: float | None = None,
        *,
        retryable: bool | None = None,
        action: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.retryable = retryable
        self.action = action
        self.details = details


class SbxTransportError(RuntimeError):
    """A bounded HTTP call failed at the transport layer (timeout/drop).

    The request may still have completed server-side: ``check`` names the
    durable GET to run before retrying (a timed-out POST can already be
    persisted — blindly re-POSTing would double-apply it). ``idempotent``
    marks calls a blind retry cannot double-apply (GETs, idempotent
    DELETE/cancel/review shapes, keyed creates). ``original`` is the httpx
    failure.
    """

    def __init__(
        self,
        method: str,
        path: str,
        *,
        check: str | None = None,
        idempotent: bool = False,
        original: httpx.TransportError | None = None,
    ) -> None:
        self.method = method
        self.path = path
        self.check = check
        self.idempotent = idempotent
        self.original = original
        if check:
            hint = (
                f"the mutation may already be persisted — run {check} to read "
                "durable state before retrying"
            )
        elif idempotent:
            hint = "safe to retry"
        else:
            hint = "read durable state before retrying"
        super().__init__(f"{method} {path} failed: {original}; {hint}")

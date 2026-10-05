"""RuntimePool — synchronous client facade over the ingress (RFC 167 §03).

Job handlers and services use this from ordinary threads; the asyncio
ingress lives in its own loop thread.
"""

from __future__ import annotations

from typing import Any

from .ingress import IngressServer


class RuntimePool:
    """Per-process runtime channel pool keyed by lease."""

    def __init__(self, ingress: IngressServer) -> None:
        self.ingress = ingress

    def start(self) -> None:
        self.ingress.start()

    def endpoint(self) -> str:
        return self.ingress.endpoint

    def attach(self, lease_id: str, timeout: float = 30.0) -> bool:
        return self.ingress.attach(lease_id, timeout) is not None

    def submit(self, lease_id: str, envelope: dict, timeout: float = 30.0) -> dict:
        """operation.submit → accepted/rejected frame."""
        return self.ingress.submit_operation(lease_id, envelope, timeout)

    def submit_for_result(self, lease_id: str, envelope: dict, timeout: float = 30.0) -> dict:
        """operation.submit → terminal operation.result frame (control ops)."""
        return self.ingress.submit_operation(lease_id, envelope, timeout, await_result=True)

    def alive(self, lease_id: str) -> bool:
        return self.ingress.attached(lease_id)

    def revoke(self, lease_id: str) -> None:
        self.ingress.revoke(lease_id)

    def stop(self) -> None:
        self.ingress.stop()

    def __enter__(self) -> RuntimePool:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

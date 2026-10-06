"""Ports consumed by the application layer; infrastructure implements them."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


class Transactions(Protocol):
    """Implemented by ``control.persistence.database.Database``."""

    def transaction(self, *, readonly: bool = False) -> AbstractContextManager[Any]: ...

    def run(self, fn: Callable[[Any], T], *, retries: int = 4) -> T: ...

    def read(self, fn: Callable[[Any], T]) -> T: ...


class HarnessCatalog(Protocol):
    """Harness manifests consumed as data (never runtime adapter execution code)."""

    def manifest(self, provider_id: str) -> dict[str, Any] | None: ...

    def capability(self, provider_id: str, name: str) -> str: ...

    def providers(self) -> list[dict[str, Any]]: ...


class SessionResolver(Protocol):
    """Resolves Project/preset/explicit settings into an effective input snapshot."""

    def resolve(
        self, uow: Any, principal: Any, workspace_id: str, body: dict[str, Any]
    ) -> dict[str, Any]: ...


# -- Execution boundary ports ------------------------------------------------------------


class RuntimeUnavailable(Exception):
    """Transport-level failure talking to sbx-runtime (no domain verdict)."""


class RuntimeRefused(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class RuntimeChannel(Protocol):
    def hello(self) -> dict[str, Any]: ...

    def op(
        self,
        kind: str,
        operation_id: str,
        session_id: str,
        payload: dict[str, Any],
        *,
        secrets: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def query(self, kind: str, **body: Any) -> dict[str, Any]: ...

    def events(self, after: int, limit: int = 500) -> dict[str, Any]: ...

    def ack(self, runtime_epoch: str, through: int) -> dict[str, Any]: ...


class RuntimeConnector(Protocol):
    def channel(self, lease: dict[str, Any]) -> RuntimeChannel: ...

    def enrollment_key(self, lease: dict[str, Any]) -> str: ...


class ExecutorBackend(Protocol):
    """RFC 03 executor port. Generic ``exec`` is deliberately absent."""

    kind: str

    def capabilities(self) -> dict[str, Any]: ...

    def allocate(self, spec: dict[str, Any], operation_id: str) -> dict[str, Any]: ...

    def lookup(self, operation_id: str, compute: dict[str, Any] | None) -> dict[str, Any] | None:
        """Rediscover the allocation made under ``operation_id`` (the effect identity).

        Returns the handle plus ``status`` (``running`` | ``terminated``) when found,
        ``None`` only when the backend authoritatively has no live allocation for the
        operation, and raises (``DomainError`` retryable) when it cannot tell. Callers
        never treat a failed lookup as absence. ``allocate`` is itself idempotent by
        ``operation_id``: a retry adopts the existing allocation instead of a twin.
        """
        ...

    def describe(
        self, handle: dict[str, Any], compute: dict[str, Any] | None
    ) -> dict[str, Any]: ...

    def connect_runtime(self, handle: dict[str, Any], compute: dict[str, Any] | None) -> str: ...

    def capture_filesystem(
        self, handle: dict[str, Any], prepared_manifest: dict[str, Any]
    ) -> dict[str, Any]: ...

    def restore(
        self, spec: dict[str, Any], snapshot_ref: str, operation_id: str
    ) -> dict[str, Any]: ...

    def terminate(
        self, handle: dict[str, Any], operation_id: str, compute: dict[str, Any] | None
    ) -> bool: ...


class CredentialBroker(Protocol):
    """Resolves plaintext only at the actual effect boundary, after reauthorization."""

    def check_inference(self, uow: Any, session: dict[str, Any]) -> dict[str, Any]: ...

    def inference(self, session: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def compute(self, session: dict[str, Any]) -> dict[str, Any] | None: ...

    def source(self, session: dict[str, Any]) -> dict[str, Any] | None: ...

    def report_health(
        self, uow: Any, connection_id: str | None, credential_version_id: str | None, health: str
    ) -> None: ...


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...


@dataclass(frozen=True)
class SealedRef:
    """Ciphertext reference handed to the vault port (key id, nonce, ciphertext)."""

    key_id: str
    nonce: bytes
    ciphertext: bytes

"""Ports consumed by the application layer; infrastructure implements them."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
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

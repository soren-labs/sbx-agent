"""Provider runtime evidence (SOR-212/SOR-215).

``sbx deploy`` writes one ``runtime/<provider>`` record per enabled provider
into the durable ``sbx-runtime`` Dict after its image step: ``ready`` when
the named image published, ``degraded`` when a local-assisted provider could
not be provisioned (a missing/wrong-version build-host CLI — a state that
must never block a Platform deploy). ``/v1/providers`` reads these records
so *runtime readiness* stays a separate signal from both the supported-
provider catalog and account connection state.

Backcompat: deployments that predate this record keep no entries — the API
reports those providers ``unknown`` rather than silently ``ready``. The
store is read-side defensive: an unreadable Dict degrades a provider to
``unknown``, it never 500s the listing.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any, Protocol, runtime_checkable

from control.config import RUNTIME_DICT_NAME, env_str

RUNTIME_DICT_ENV = "SBX_RUNTIME_DICT"
RUNTIME_KEY_PREFIX = "runtime/"
RUNTIME_RECORD_SCHEMA = "sbx-runtime/provider@1"

STATUS_READY = "ready"
STATUS_DEGRADED = "degraded"


@dataclass(frozen=True)
class ProviderRuntimeRecord:
    """Deploy-written readiness evidence for one provider."""

    provider: str
    status: str  # STATUS_READY | STATUS_DEGRADED
    image: str
    version: str | None
    detail: str
    updated_at: str  # ISO-8601

    def to_dict(self) -> dict[str, Any]:
        return {"schema": RUNTIME_RECORD_SCHEMA, **asdict(self)}

    @staticmethod
    def from_dict(data: Any) -> ProviderRuntimeRecord | None:
        if not isinstance(data, dict) or not isinstance(data.get("provider"), str):
            return None
        try:
            return ProviderRuntimeRecord(
                provider=data["provider"],
                status=str(data.get("status") or ""),
                image=str(data.get("image") or ""),
                version=data.get("version"),
                detail=str(data.get("detail") or ""),
                updated_at=str(data.get("updated_at") or ""),
            )
        except Exception:
            return None


@runtime_checkable
class RuntimeEvidenceStore(Protocol):
    """Read surface for deploy-written runtime records."""

    def get(self, provider: str) -> ProviderRuntimeRecord | None:
        """The latest record for ``provider``, or None."""


class InMemoryRuntimeStore:
    """Empty/injectable store — local mode and tests."""

    def __init__(self, records: tuple[ProviderRuntimeRecord, ...] = ()) -> None:
        self._records = {r.provider: r for r in records}

    def get(self, provider: str) -> ProviderRuntimeRecord | None:
        return self._records.get(provider)


class ModalDictRuntimeStore:
    """Production store backed by ``modal.Dict`` — lazy-imports modal."""

    def __init__(self, name: str | None = None) -> None:
        self._name = name or env_str(RUNTIME_DICT_ENV, RUNTIME_DICT_NAME)
        self._dict: Any = None

    def _d(self) -> Any:
        if self._dict is None:
            import modal

            self._dict = modal.Dict.from_name(self._name, create_if_missing=True)
        return self._dict

    def get(self, provider: str) -> ProviderRuntimeRecord | None:
        try:
            raw = self._d().get(f"{RUNTIME_KEY_PREFIX}{provider}")
        except Exception:
            return None  # an unreadable Dict degrades to ``unknown``, never 500
        return ProviderRuntimeRecord.from_dict(raw)


def select_runtime_store(*, backend: str | None = None) -> RuntimeEvidenceStore:
    """Pick the runtime-evidence store the same way other stores do.

    ``SBX_BACKEND=modal`` → the ``sbx-runtime`` Dict; otherwise an empty
    in-memory store (no deploy evidence → every provider ``unknown``).
    """
    kind = backend if backend is not None else os.environ.get("SBX_BACKEND", "local")
    if kind == "modal":
        return ModalDictRuntimeStore()
    return InMemoryRuntimeStore()

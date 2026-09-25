"""``control.runtime_state`` — deploy-written runtime evidence store
(SOR-212/SOR-215)."""

from __future__ import annotations

import pytest
from control.runtime_state import (
    RUNTIME_RECORD_SCHEMA,
    InMemoryRuntimeStore,
    ProviderRuntimeRecord,
    select_runtime_store,
)


def _record(provider: str = "codex", status: str = "ready") -> ProviderRuntimeRecord:
    return ProviderRuntimeRecord(
        provider=provider,
        status=status,
        image="sbx-runtime",
        version="1.2.3",
        detail="",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def test_record_round_trip() -> None:
    record = _record()
    data = record.to_dict()
    assert data["schema"] == RUNTIME_RECORD_SCHEMA
    parsed = ProviderRuntimeRecord.from_dict(data)
    assert parsed == record


def test_record_from_dict_tolerates_garbage() -> None:
    assert ProviderRuntimeRecord.from_dict(None) is None
    assert ProviderRuntimeRecord.from_dict("degraded") is None
    assert ProviderRuntimeRecord.from_dict({"status": "ready"}) is None


def test_in_memory_store() -> None:
    store = InMemoryRuntimeStore((_record("grok", "degraded"),))
    assert store.get("grok").status == "degraded"
    assert store.get("codex") is None


def test_select_runtime_store_backend() -> None:
    """Local mode yields an in-memory store; modal defers the lazy client."""
    store = select_runtime_store(backend="local")
    assert isinstance(store, InMemoryRuntimeStore)
    from control.runtime_state import ModalDictRuntimeStore

    store = select_runtime_store(backend="modal")
    assert isinstance(store, ModalDictRuntimeStore)
    # env override names the dict (default sbx-runtime)
    assert store._name == "sbx-runtime"


def test_select_runtime_store_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SBX_RUNTIME_DICT", "custom-runtime")
    monkeypatch.setenv("SBX_BACKEND", "modal")
    store = select_runtime_store()
    assert store._name == "custom-runtime"


def test_modal_store_get_tolerates_failures() -> None:
    """An unreadable Dict degrades to None — callers map it to ``unknown``."""
    from control.runtime_state import ModalDictRuntimeStore

    store = ModalDictRuntimeStore("missing-dict-for-test")
    # modal isn't installed in the unit env — the lazy import fails and
    # ``get`` must still return None, never raise.
    assert store.get("codex") is None

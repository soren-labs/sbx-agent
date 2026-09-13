"""InMemoryStore round-trip (ModalDictStore is production-only, not instantiated)."""

from __future__ import annotations

from datetime import UTC, datetime

from control.store import (
    InMemoryStore,
    SessionRecord,
    empty_usage,
    merge_usage,
    record_from_dict,
    record_to_dict,
)


def test_inmemory_put_get_list_delete() -> None:
    store = InMemoryStore()
    now = datetime.now(UTC)
    rec = SessionRecord(
        id="abc",
        title="t",
        status="idle",
        created_at=now,
        updated_at=now,
        model="gpt-5.6-luna",
        turns=1,
        usage=empty_usage(),
        messages=[{"role": "user", "text": "hi", "turn_id": "turn-1", "ts": now.isoformat()}],
        owner="sbx",
        sandbox_id="sb",
        sandbox_root="/tmp/x",
        last_activity_at=now,
    )
    store.put(rec)
    got = store.get("abc")
    assert got is not None
    assert got.id == "abc"
    assert got.messages[0]["text"] == "hi"
    assert got.handle() is not None
    assert len(store.list_all()) == 1
    store.delete("abc")
    assert store.get("abc") is None


def test_record_dict_roundtrip() -> None:
    now = datetime(2026, 9, 13, tzinfo=UTC)
    rec = SessionRecord(
        id="z",
        title="t",
        status="closed",
        created_at=now,
        updated_at=now,
        model="m",
        turns=0,
        usage=empty_usage(),
        messages=[],
        owner="sbx",
        ended_at=now,
    )
    again = record_from_dict(record_to_dict(rec))
    assert again.status == "closed"
    assert again.ended_at == now


def test_merge_usage() -> None:
    base = empty_usage()
    merged = merge_usage(
        base,
        {
            "input_tokens": 10,
            "cached_input_tokens": 4,
            "output_tokens": 2,
            "reasoning_output_tokens": 1,
        },
    )
    assert merged["input_tokens"] == 10
    assert merged["reasoning_output_tokens"] == 1
    twice = merge_usage(merged, {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 1})
    assert twice["input_tokens"] == 15

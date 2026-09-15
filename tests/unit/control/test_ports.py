"""P2 WP0 port shells: Protocol conformance and in-memory fakes (SOR-59)."""

from __future__ import annotations

import pytest
from control.ports import (
    Account,
    AccountRegistry,
    ApiKeyStore,
    ScheduleDecision,
    Scheduler,
)
from runtime.runner.adapter import AgentAdapter, CodexAdapter, get_adapter
from tests.fakes.fake_ports import (
    InMemoryAccountRegistry,
    InMemoryApiKeyStore,
    InMemoryScheduler,
)


def _account(id: str, provider: str = "codex", **kw: object) -> Account:
    return Account(id=id, provider=provider, label=id, **kw)  # type: ignore[arg-type]


class TestAdapterRegistry:
    def test_codex_registered(self) -> None:
        adapter = get_adapter("codex")
        assert isinstance(adapter, AgentAdapter)
        assert adapter.provider == "codex"

    def test_antigravity_registered(self) -> None:
        adapter = get_adapter("antigravity")
        assert isinstance(adapter, AgentAdapter)
        assert adapter.provider == "antigravity"

    def test_devin_registered(self) -> None:
        adapter = get_adapter("devin")
        assert isinstance(adapter, AgentAdapter)
        assert adapter.provider == "devin"

    def test_other_providers_not_yet_registered(self) -> None:
        for provider in ("grok", "opencode"):
            with pytest.raises(KeyError):
                get_adapter(provider)

    def test_unknown_provider(self) -> None:
        with pytest.raises(KeyError):
            get_adapter("nope")


class TestCodexAdapter:
    def test_translate_passthrough(self) -> None:
        adapter = CodexAdapter()
        line = '{"type": "thread.started", "thread_id": "t-1"}'
        events = adapter.translate(line)
        assert events == [{"type": "thread.started", "thread_id": "t-1"}]

    def test_translate_bad_line_returns_empty(self) -> None:
        adapter = CodexAdapter()
        assert adapter.translate("not json") == []

    def test_extract_session_id(self) -> None:
        adapter = CodexAdapter()
        events = [
            {"type": "turn.started"},
            {"type": "thread.started", "thread_id": "t-9"},
        ]
        assert adapter.extract_session_id(events) == "t-9"
        assert adapter.extract_session_id([{"type": "turn.started"}]) is None

    def test_health_from(self) -> None:
        adapter = CodexAdapter()
        assert adapter.health_from(0, "") == "ok"
        assert adapter.health_from(1, "401 Unauthorized") == "auth_invalid"
        assert adapter.health_from(1, "429 too many requests") == "rate_limited"
        assert adapter.health_from(1, "segfault") == "unknown"


class TestInMemoryAccountRegistry:
    def test_protocol_conformance(self) -> None:
        assert isinstance(InMemoryAccountRegistry(), AccountRegistry)

    def test_crud_and_credential_blob(self) -> None:
        reg = InMemoryAccountRegistry()
        reg.put(_account("a1"))
        reg.put_credential_blob("a1", {"provider": "codex", "files": {".codex/auth.json": "{}"}})
        assert reg.get("a1").status == "active"  # type: ignore[union-attr]
        assert reg.get_credential_blob("a1") == {
            "provider": "codex",
            "files": {".codex/auth.json": "{}"},
        }
        reg.mark_status("a1", "cooling", cooldown_until="2030-01-01T00:00:00Z")
        assert reg.get("a1").status == "cooling"  # type: ignore[union-attr]
        reg.remove("a1")
        assert reg.get("a1") is None
        assert reg.get_credential_blob("a1") is None
        with pytest.raises(KeyError):
            reg.mark_status("a1", "active")


class TestInMemoryScheduler:
    def test_protocol_conformance(self) -> None:
        assert isinstance(InMemoryScheduler(InMemoryAccountRegistry()), Scheduler)

    def test_invalid_provider(self) -> None:
        sched = InMemoryScheduler(InMemoryAccountRegistry())
        assert sched.decide(provider="nope").error == "invalid_provider"

    def test_auto_lru_and_exhaustion(self) -> None:
        reg = InMemoryAccountRegistry()
        reg.put(_account("a1"))
        reg.put(_account("a2"))
        sched = InMemoryScheduler(reg)
        d1 = sched.decide(provider="codex")
        assert d1.account is not None
        reg.set_running(d1.account.id, 1)  # max_concurrent default is 1
        d2 = sched.decide(provider="codex")
        assert d2.account is not None
        assert d2.account.id != d1.account.id
        reg.set_running(d2.account.id, 1)
        d3 = sched.decide(provider="codex")
        assert d3.error == "provider_exhausted"
        assert d3.retry_after is not None

    def test_named_account_busy_and_unavailable(self) -> None:
        reg = InMemoryAccountRegistry()
        reg.put(_account("a1"))
        sched = InMemoryScheduler(reg)
        reg.set_running("a1", 1)
        assert sched.decide(provider="codex", account="a1").error == "account_busy"
        reg.set_running("a1", 0)
        reg.mark_status("a1", "disabled")
        assert sched.decide(provider="codex", account="a1").error == "account_unavailable"
        assert sched.decide(provider="codex", account="missing").error == "account_unavailable"

    def test_decision_is_a_schedule_decision(self) -> None:
        sched = InMemoryScheduler(InMemoryAccountRegistry())
        assert isinstance(sched.decide(provider="codex"), ScheduleDecision)


class TestInMemoryApiKeyStore:
    def test_protocol_conformance(self) -> None:
        assert isinstance(InMemoryApiKeyStore(), ApiKeyStore)

    def test_create_lookup_revoke(self) -> None:
        store = InMemoryApiKeyStore()
        record, token = store.create(label="ci", scopes=("agents",))
        assert token.startswith("sbx_")
        found = store.lookup(token)
        assert found is not None and found.id == record.id
        assert store.revoke(record.id) is True
        assert store.lookup(token) is None
        assert store.revoke(record.id) is False
        assert store.lookup("sbx_nonexistent") is None

    def test_only_hash_stored(self) -> None:
        store = InMemoryApiKeyStore()
        record, token = store.create()
        assert token not in record.key_hash
        assert len(record.key_hash) == 64

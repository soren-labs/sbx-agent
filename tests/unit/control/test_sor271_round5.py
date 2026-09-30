"""SOR-271 round-5 repair invariants: the sweep must SEE every record and
dead sessions must not hold capacity slots.

Pinned from the round-5 production gate on exact-main ``eb86d68``: the cron
verifiably ran every ~5min (ops heartbeat advancing, ``suspended`` actions
emitted) yet four ``idle`` agents — 26-33min past the 300s timeout, with
live sandboxes and materializable handles — were never reaped: no
``reap_error``, no ``platform_loss``, no ``orphan_terminate``, and no
``updated_at`` writes between last-activity and manual cleanup. A record
invisible to the listing is invisible to the sweep forever.

Defects fixed (each test names the production evidence it pins):

1. ``store.list_all``'s indexed manifest can silently drop records (a
   documented cross-container RMW loss; ``rebuild_index`` heals only
   opportunistically and its failure is silent). The sweep now
   enumerates authoritatively — ``list_all_scan`` when the store offers
   it — for both the records pass and the orphan pass.
2. ``suspended`` agents own no live sandbox but kept their scheduler
   lease forever (``_on_action`` skipped release for them) and still
   counted in ``session_running_source`` — pinned the global cap while
   holding nothing.
3. ``AccountLease`` objects are in-process: the cron's
   ``release_lease_for_action`` is a no-op on the web process that owns
   them, so dead leases must also decay where they live —
   ``V1State.reconcile_leases`` runs lazily on the bind path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from control.api_v1.state import V1State
from control.backend import LocalProcessBackend, SandboxSpec
from control.ports import Account
from control.reaper import reap, sweep_plane
from control.scheduler import (
    AccountScheduler,
    ScheduleRefused,
    session_running_source,
)
from control.service import ControlPlane
from control.store import InMemoryStore, SessionRecord, empty_usage
from tests.fakes.fake_ports import InMemoryAccountRegistry

_NOW = datetime(2026, 9, 30, 2, 0, tzinfo=UTC)
_STALE = _NOW - timedelta(seconds=3600)


def _record(
    *,
    session_id: str,
    status: str,
    handle_id: str | None = None,
    account_id: str | None = None,
    last: datetime | None = None,
) -> SessionRecord:
    at = last if last is not None else _NOW
    tags = {"session_id": session_id, "owner": "sbx"}
    if account_id is not None:
        tags["account_id"] = account_id
    return SessionRecord(
        id=session_id,
        title="t",
        status=status,
        created_at=at,
        updated_at=at,
        model="m",
        turns=0,
        usage=empty_usage(),
        messages=[],
        owner="sbx",
        sandbox_id=handle_id,
        sandbox_root="/tmp/x" if handle_id else None,
        sandbox_tags=tags,
        last_activity_at=at,
    )


def _sandbox(backend: LocalProcessBackend, session_id: str):
    return backend.create(SandboxSpec(tags={"session_id": session_id, "owner": "sbx"}))


class _FakeLease:
    """Stand-in for ``scheduler.AccountLease`` (release observable)."""

    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class TestAuthoritativeListing:
    def test_reap_sees_records_the_index_listing_drops(self) -> None:
        """A manifest-blind ``list_all`` is why the zombies never reaped:
        the sweep must enumerate durable truth (``list_all_scan``) so a
        record missing from the index cannot stay ``idle`` + live
        forever. Pre-fix the record below never entered the records
        pass and its sandbox never hit the orphan pass."""
        backend, store = LocalProcessBackend(), InMemoryStore()
        handle = _sandbox(backend, "zombie")
        store.put(_record(session_id="zombie", status="idle", handle_id=handle.id, last=_STALE))

        class BlindStore(InMemoryStore):
            def __init__(self, inner: InMemoryStore) -> None:
                super().__init__()
                self._inner = inner

            def get(self, session_id: str):
                return self._inner.get(session_id)

            def put(self, rec: SessionRecord) -> None:
                self._inner.put(rec)

            def list_all(self) -> list[SessionRecord]:
                # The production failure shape: indexed listing silently
                # returns nothing while the dict still holds the record.
                return []

            def list_all_scan(self) -> list[SessionRecord]:
                return self._inner.list_all()

        blind = BlindStore(store)
        actions = reap(blind, backend, _NOW, idle_timeout_s=300)
        assert store.get("zombie").status == "timed_out"
        assert backend.poll(handle).alive is False
        assert [a.kind for a in actions] == ["timed_out"]

    def test_orphan_pass_uses_the_same_authoritative_listing(self) -> None:
        """``listed`` (the orphan pass's session map) must see the same
        records — a terminal record's surviving sandbox is
        ``terminal_cleanup``, not ``orphan_terminate``."""
        backend, store = LocalProcessBackend(), InMemoryStore()
        handle = _sandbox(backend, "dead")
        store.put(_record(session_id="dead", status="closed", handle_id=handle.id, last=_STALE))

        class BlindStore(InMemoryStore):
            def __init__(self, inner: InMemoryStore) -> None:
                super().__init__()
                self._inner = inner

            def get(self, session_id: str):
                return self._inner.get(session_id)

            def put(self, rec: SessionRecord) -> None:
                self._inner.put(rec)

            def list_all(self) -> list[SessionRecord]:
                return []

            def list_all_scan(self) -> list[SessionRecord]:
                return self._inner.list_all()

        actions = reap(BlindStore(store), backend, _NOW, idle_timeout_s=300)
        assert backend.poll(handle).alive is False
        assert [a.kind for a in actions] == ["terminal_cleanup"]

    def test_sweep_summary_reports_what_the_sweep_saw(self) -> None:
        """Observability that distinguishes "reaper ran, nothing to do"
        from "reaper ran but enumerated nothing" — the round-5 evidence
        hole that made the zombie cause invisible for days."""
        backend, store = LocalProcessBackend(), InMemoryStore()
        handle = _sandbox(backend, "fresh")
        # ``sweep_plane`` judges staleness at wall-clock now — a fresh
        # idle must sit inside the retention window.
        store.put(
            _record(
                session_id="fresh",
                status="idle",
                handle_id=handle.id,
                last=datetime.now(UTC),
            )
        )
        plane = ControlPlane(backend, store, ["fake-runner"], max_concurrent=8, clock=lambda: _NOW)
        summary = sweep_plane(plane, log=lambda _line: None)
        assert summary["records_seen"] == 1
        assert summary["handles_seen"] >= 1
        assert summary["skipped"].get("idle_fresh") == 1
        assert summary["skipped"].get("bound_live") == 1


class TestSuspendedHoldsNoCapacity:
    def test_suspended_action_releases_the_lease(self) -> None:
        """A ``suspended`` agent owns no live sandbox: its lease must free
        like every other reaped state — the round-5 wedge where ~8 ops
        pinned the global cap. Pre-fix ``_on_action`` skipped release for
        ``suspended`` on the theory it stays recoverable; recoverability
        lives in the durable checkpoint, not in a capacity slot."""
        backend, store = LocalProcessBackend(), InMemoryStore()
        handle = _sandbox(backend, "idle1")
        store.put(_record(session_id="idle1", status="idle", handle_id=handle.id, last=_STALE))
        plane = ControlPlane(backend, store, ["fake-runner"], max_concurrent=8, clock=lambda: _NOW)
        v1 = V1State()
        lease = _FakeLease()
        v1.set_lease("idle1", lease)

        class Suspends:
            def suspend(self, _rec, _handle) -> bool:
                return True

        plane.checkpoints = Suspends()  # type: ignore[attr-defined]
        summary = sweep_plane(plane, v1_state=v1, log=lambda _line: None)
        assert store.get("idle1").status == "suspended"
        assert summary["action_kinds"] == {"suspended": 1}
        assert lease.released is True
        assert "idle1" not in v1.leases

    def test_session_running_source_excludes_suspended(self) -> None:
        """The scheduler's durable count must agree with open_session's
        cap (excludes suspended) — a suspended record pinned external
        running forever, so max(leases, external) never freed the slot."""
        store = InMemoryStore()
        store.put(_record(session_id="susp", status="suspended", account_id="a"))
        store.put(_record(session_id="live", status="idle", handle_id="sbx-1", account_id="a"))
        count = session_running_source(store)
        assert count("a") == 1


class TestLazyLeaseDecay:
    def test_reconcile_leases_frees_dead_sessions(self) -> None:
        """Terminal, suspended, and record-gone sessions release; only the
        live lease survives — the in-process half of the zombie wedge the
        cron cannot reach."""
        v1, store = V1State(), InMemoryStore()
        live = _FakeLease()
        v1.set_lease("live", live)
        for sid in ("closed-s", "susp-s", "gone-s"):
            v1.set_lease(sid, _FakeLease())
        store.put(_record(session_id="live", status="idle", handle_id="sbx-l"))
        store.put(_record(session_id="closed-s", status="closed"))
        store.put(_record(session_id="susp-s", status="suspended"))

        released = v1.reconcile_leases(store, interval_s=0)
        assert released == 3
        assert list(v1.leases) == ["live"]
        assert live.released is False

    def test_reconcile_leases_keeps_leases_on_store_failure(self) -> None:
        """A store blip must not free slots — unknown state keeps the
        lease for the next pass."""
        v1 = V1State()
        lease = _FakeLease()
        v1.set_lease("s1", lease)

        class DownStore:
            def get(self, _sid: str):
                raise RuntimeError("dict unreachable")

        assert v1.reconcile_leases(DownStore(), interval_s=0) == 0
        assert v1.leases["s1"] is lease

    def test_reconcile_leases_is_interval_throttled(self) -> None:
        v1 = V1State()
        lease = _FakeLease()
        v1.set_lease("dead", lease)

        class RecordStore:
            def get(self, _sid: str):
                return None

        assert v1.reconcile_leases(RecordStore(), interval_s=0) == 1
        v1.set_lease("dead2", _FakeLease())
        # Inside the throttle window the pass is skipped entirely.
        assert v1.reconcile_leases(RecordStore()) == 0
        assert "dead2" in v1.leases
        assert v1.reconcile_leases(RecordStore(), interval_s=0) == 1
        assert "dead2" not in v1.leases

    def test_dead_lease_unwedges_acquire(self) -> None:
        """The gate's exact symptom: a finished/cancelled session's lease
        pinned the global cap so the next bind refused — reconciling on
        the bind path frees the slot where the lease actually lives."""
        registry = InMemoryAccountRegistry()
        registry.put(
            Account(
                id="acct",
                provider="codex",
                label="acct",
                status="active",
                max_concurrent=4,
                created_at=datetime.now(UTC).isoformat(),
            )
        )
        store = InMemoryStore()
        scheduler = AccountScheduler(
            registry, max_global=1, external_running=session_running_source(store)
        )
        lease = scheduler.acquire(provider="codex", account="acct")
        v1 = V1State()
        v1.set_lease("finished", lease)
        store.put(_record(session_id="finished", status="closed", account_id="acct"))

        with pytest.raises(ScheduleRefused):
            scheduler.acquire(provider="codex", account="acct")

        v1.reconcile_leases(store, interval_s=0)
        assert "finished" not in v1.leases
        assert scheduler.acquire(provider="codex", account="acct")

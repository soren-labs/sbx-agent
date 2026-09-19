"""Idle / orphan reaper as a pure function (clock injected via ``now``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from control.accounts import cooldown_expired
from control.backend import SandboxBackend, SandboxHandle
from control.config import TERMINAL_STATUSES, lifecycle_config
from control.ports import AccountRegistry
from control.store import SessionRecord, SessionStore


@dataclass(frozen=True)
class ReapAction:
    kind: str
    session_id: str | None
    sandbox_id: str | None
    account_id: str | None = None


def reap(
    store: SessionStore,
    backend: SandboxBackend,
    now: datetime,
    *,
    idle_timeout_s: int | None = None,
    create_grace_s: int | None = None,
    run_grace_s: int | None = None,
    account_registry: AccountRegistry | None = None,
    on_action: Callable[[ReapAction], None] | None = None,
) -> list[ReapAction]:
    """Reconcile Dict records with live sandboxes.

    Rules (SOR-31 + P0 + SOR-80):
    * ``creating`` record without ``sandbox_id``: younger than
      ``create_grace_s`` → in-flight create, left alone; older → ``lost``
    * ``creating`` record with a live sandbox that has not settled within
      ``create_grace_s`` of its last update → ``lost`` (the provisioner is
      gone — e.g. control-plane restart mid-create — so nothing will ever
      finish it; the orphan pass then reclaims the sandbox)
    * ``running`` record stale beyond the runner's own ``--max-seconds``
      bound plus ``run_grace_s`` → ``lost`` (the turn watcher is
      in-process; a control-plane cutover mid-turn strands the record
      ``running`` on a live sandbox forever, holding the account slot)
    * idle longer than ``idle_timeout_s`` (the post-session retention,
      SOR-135) and sandbox still alive → terminate + ``timed_out``
    * record exists, sandbox gone, status was idle → ``timed_out`` (native
      ``Sandbox.create(idle_timeout=)`` fired — a separate resolved knob,
      ``sandbox_idle_timeout_s``, that bounds a live sandbox instead)
    * record exists, sandbox gone, status was creating/running → ``lost``
    * live sandbox whose record is terminal → retry terminate (``terminal_cleanup``)
    * sandbox exists with no Dict record → terminate (``orphan_terminate``)
    * sandbox tagged with a session whose record is still in-flight
      (``creating`` + unbound) → leave alone; the binding lands shortly
    * ``account_registry`` sweep (SOR-63): a ``cooling`` account whose
      ``cooldown_until`` passed → ``active`` (``account_recovered``). Slot
      accounting stays lazy — the scheduler recovers on pick; this sweep
      makes recovery visible without waiting for traffic.

    ``on_action`` (optional) is invoked once per emitted action — the
    production cron wires it to ``/v1`` lease release (SOR-80).

    Unset bounds resolve from ``lifecycle_config`` (SOR-132/SOR-134), so
    every caller — cron, local sweep, gate — shares the deploy's resolved
    values instead of the contract defaults.
    """
    lifecycle = lifecycle_config()
    if idle_timeout_s is None:
        idle_timeout_s = lifecycle.idle_timeout_s
    if create_grace_s is None:
        create_grace_s = lifecycle.create_grace_s
    if run_grace_s is None:
        run_grace_s = lifecycle.run_stale_s
    actions: list[ReapAction] = []

    def emit(
        kind: str,
        session_id: str | None,
        sandbox_id: str | None,
        account_id: str | None = None,
    ) -> None:
        action = ReapAction(kind, session_id, sandbox_id, account_id)
        actions.append(action)
        if on_action is not None:
            on_action(action)

    if account_registry is not None:
        for acct in account_registry.list():
            if not cooldown_expired(acct, now):
                continue
            try:
                account_registry.mark_status(acct.id, "active")
            except (KeyError, ValueError):
                # Missing — or a stored record whose id fails account_id
                # validation (SOR-105): never usable, leave it.
                continue
            emit("account_recovered", None, None, account_id=acct.id)

    for rec in store.list_all():
        if rec.status in TERMINAL_STATUSES:
            continue
        if rec.status == "creating" and not rec.sandbox_id:
            # Record published before the sandbox bound (SOR-80 create order).
            age_s = (now - rec.created_at).total_seconds()
            if age_s < create_grace_s:
                continue
            rec.status = "lost"
            rec.ended_at = now
            rec.updated_at = now
            store.put(rec)
            emit("lost", rec.id, None)
            continue
        handle = rec.handle()
        poll = backend.poll(handle) if handle is not None else None
        alive = bool(poll and poll.alive)
        last = rec.last_activity_at or rec.updated_at
        idle_expired = (now - last).total_seconds() >= idle_timeout_s

        if not alive:
            rec.status = "timed_out" if rec.status == "idle" else "lost"
            rec.ended_at = now
            rec.updated_at = now
            rec.current_turn_id = None
            rec.current_turn_n = None
            store.put(rec)
            emit(rec.status, rec.id, rec.sandbox_id)
            continue

        if rec.status == "creating":
            # Bound but still ``creating`` past the create grace window: the
            # provisioning worker is gone (control-plane restart/crash) or
            # init is stuck — no one will ever settle the record, and the
            # sandbox would bill forever. Mark ``lost``; the orphan pass
            # below reclaims the sandbox and ``on_action`` frees the lease.
            if (now - rec.updated_at).total_seconds() >= create_grace_s:
                rec.status = "lost"
                rec.ended_at = now
                rec.updated_at = now
                store.put(rec)
                emit("lost", rec.id, rec.sandbox_id)
            continue

        if rec.status == "running":
            if (now - rec.updated_at).total_seconds() >= run_grace_s:
                # Mark terminal *before* terminate (close() ordering): the
                # slot frees immediately, and a failed terminate is retried
                # by the terminal_cleanup pass on the next sweep.
                rec.status = "lost"
                rec.ended_at = now
                rec.updated_at = now
                rec.current_turn_id = None
                rec.current_turn_n = None
                store.put(rec)
                emit("lost", rec.id, rec.sandbox_id)
                if handle is not None:
                    try:
                        backend.terminate(handle)
                    except Exception:
                        emit("cleanup_failed", rec.id, rec.sandbox_id)
            continue

        if rec.status == "idle" and idle_expired and handle is not None:
            backend.terminate(handle)
            rec.status = "timed_out"
            rec.ended_at = now
            rec.updated_at = now
            rec.current_turn_id = None
            rec.current_turn_n = None
            store.put(rec)
            emit("timed_out", rec.id, rec.sandbox_id)

    records = {rec.id: rec for rec in store.list_all()}
    bound_live = {
        rec.sandbox_id
        for rec in records.values()
        if rec.sandbox_id and rec.status not in TERMINAL_STATUSES
    }
    for handle in backend.list():
        if handle.id in bound_live:
            continue
        rec = _session_record(records, handle)
        if rec is not None and rec.status not in TERMINAL_STATUSES:
            # Same-session record without a bound sandbox id → in-flight
            # create in the record-publication window; never orphan-kill it.
            if rec.sandbox_id is None:
                continue
            kind = "orphan_terminate"
            session_id = None
        elif rec is not None and rec.sandbox_id == handle.id:
            # Terminal record whose earlier terminate failed: retry cleanup.
            kind = "terminal_cleanup"
            session_id = rec.id
        else:
            kind = "orphan_terminate"
            session_id = None
        try:
            backend.terminate(handle)
        except Exception:
            emit("cleanup_failed", session_id, handle.id)
            continue
        emit(kind, session_id, handle.id)

    return actions


def _session_record(
    records: dict[str, SessionRecord], handle: SandboxHandle
) -> SessionRecord | None:
    """The record owning ``handle``: by ``session_id`` tag, else by bound id."""
    session_id = (handle.tags or {}).get("session_id")
    if session_id:
        rec = records.get(session_id)
        if rec is not None:
            return rec
    return next((r for r in records.values() if r.sandbox_id == handle.id), None)

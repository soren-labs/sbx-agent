"""Idle / orphan reaper as a pure function (clock injected via ``now``)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from control.accounts import cooldown_expired
from control.backend import SandboxBackend, SandboxHandle
from control.config import TERMINAL_STATUSES, lifecycle_config
from control.ports import AccountRegistry
from control.store import SessionRecord, SessionStore

# Upper bound on one sandbox checkpoint per sweep record. A filesystem
# suspend that never returns starves every record after it — and the
# orphan pass — on every tick, which is a reaper that never reaps
# (SOR-271 round-3). Past the bound the wedged agent takes the ordinary
# terminate + ``timed_out`` path; a late-landing checkpoint is a wasted
# op, not a stuck sweep.
_SUSPEND_BOUND_S = 120

# SOR-271 round-4: the round-3 fix isolated *throwing* remote calls, but
# the production wedge was a remote call that never *returns* — a sandbox
# poll/read on a wedged sandbox or a stalled Dict RPC starves the sweep
# identically and silently. Every remote call on the invocation path is
# now time-bounded; a bound that trips degrades to "this record/op
# skipped", never to a dead tick.
_POLL_BOUND_S = 30
_LIST_BOUND_S = 60
_PUT_BOUND_S = 30
_TERMINATE_BOUND_S = 60
_ON_ACTION_BOUND_S = 60
_REGISTRY_BOUND_S = 30
_REBUILD_BOUND_S = 120
_RECONCILE_BOUND_S = 120
# Soft wall for one whole tick — below the 5-minute cron period so a tick
# can never run into (or past) the next invocation.
_SWEEP_BOUND_S = 240


def _bounded_call(fn: Callable[[], Any], timeout_s: float) -> Any:
    """Run ``fn`` on a daemon thread; return its value or ``None`` on timeout."""
    result: list[Any] = []

    def _run() -> None:
        try:
            result.append(fn())
        except Exception:
            return

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive() or not result:
        return None
    return result[0]


@dataclass(frozen=True)
class ReapAction:
    kind: str
    session_id: str | None
    sandbox_id: str | None
    account_id: str | None = None


def _list_all(store: SessionStore) -> list[SessionRecord]:
    """Authoritative session enumeration for life-safety decisions.

    ``SessionStore.list_all`` may be served by the ModalDictStore's
    indexed manifest, which can silently drop records — a cross-container
    RMW race the periodic rebuild only heals opportunistically. A record
    missing from the listing is invisible to this sweep forever: it stays
    ``idle``, keeps its live sandbox, and holds a capacity slot while the
    API keeps reporting it (SOR-271 round-5 zombie agents). When the
    store offers an unindexed ``list_all_scan`` (a full ``items()``
    enumeration), prefer it; in-memory stores fall back to ``list_all``.
    """
    scan = getattr(store, "list_all_scan", None)
    if callable(scan):
        return list(scan())
    return list(store.list_all())


def reap(
    store: SessionStore,
    backend: SandboxBackend,
    now: datetime,
    *,
    idle_timeout_s: int | None = None,
    create_grace_s: int | None = None,
    run_grace_s: int | None = None,
    account_registry: AccountRegistry | None = None,
    checkpoints: Any = None,
    on_action: Callable[[ReapAction], None] | None = None,
    on_scan: Callable[[list[SessionRecord]], None] | None = None,
    deadline_s: float | None = None,
    stats: dict[str, Any] | None = None,
) -> list[ReapAction]:
    """Reconcile Dict records with live sandboxes.

    Rules (SOR-31 + P0 + SOR-80 + SOR-180):
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
    * ``suspended`` record (SOR-180): skipped entirely — it owns no live
      sandbox and a follow-up message restores it from its checkpoint
    * idle longer than ``idle_timeout_s`` (the post-session retention,
      SOR-135) and sandbox still alive → with ``checkpoints`` wired,
      filesystem-checkpoint the agent then terminate + ``suspended``
      (recoverable; lease deliberately kept); without checkpoints (or when
      the checkpoint fails) → terminate + ``timed_out`` as before
    * record exists, sandbox gone, status was idle → ``timed_out`` (native
      ``Sandbox.create(idle_timeout=)`` fired — a separate resolved knob,
      ``sandbox_idle_timeout_s``, that bounds a live sandbox instead).
      With ``checkpoints`` wired this is an explicit ``platform_loss``
      diagnosis instead: an agent with a usable checkpoint heals to
      ``suspended`` (the release transition half-finished); without one it
      goes ``lost`` — uncheckpointed platform loss, not a policy expiry
    * record exists, sandbox gone, status was creating/running → ``lost``
    * live sandbox whose record is terminal → retry terminate (``terminal_cleanup``)
    * sandbox exists with no Dict record → terminate (``orphan_terminate``)
    * sandbox tagged with a session whose record is still in-flight
      (``creating`` + unbound) → leave alone; the binding lands shortly
    * ``account_registry`` sweep (SOR-63): a ``cooling`` account whose
      ``cooldown_until`` passed → ``active`` (``account_recovered``). Slot
      accounting stays lazy — the scheduler recovers on pick; this sweep
      makes recovery visible without waiting for traffic.

    ``checkpoints`` (optional, SOR-180) is the ``CheckpointService`` — the
    reaper calls ``suspend(rec, handle)`` / ``has_checkpoint(id)`` /
    ``fail(id, error)`` on it, duck-typed.

    ``on_action`` (optional) is invoked once per emitted action — the
    production cron wires it to ``/v1`` lease release (SOR-80). A
    ``suspended`` agent owns no live sandbox: its lease is released like
    every other reaped state — recoverability lives in the durable
    checkpoint, while an unreleased slot wedges new binds at the global
    cap (SOR-271 round-5). A throwing callback is isolated: it produces a
    ``reap_error`` action and the sweep continues — one bad remote call
    (``settle_orphaned_runs`` hits the run store) must never abort the
    records loop or skip the orphan pass, which is the pass that reclaims
    zombie sandboxes (SOR-271 round-3: callbacks are part of the
    invocation path, not the rules).

    SOR-271 round-4: every remote call on this path — store list/put,
    backend poll/list/terminate, registry ops, ``rebuild_index``, and
    ``on_action`` itself — is *time*-bounded, not just exception-guarded:
    a call that never returns is a dead tick on every cron invocation,
    which is exactly the "cron fires yet nothing ever reaps" production
    symptom. ``deadline_s`` adds a soft wall for the whole sweep (checked
    per record/handle); on expiry the sweep emits ``sweep_deadline`` and
    returns what it settled instead of running into the next tick.

    Unset bounds resolve from ``lifecycle_config`` (SOR-132/SOR-134), so
    every caller — cron, local sweep, gate — shares the deploy's resolved
    values instead of the contract defaults.

    ``stats`` (optional) is a dict the sweep fills in place:
    ``records_seen``/``handles_seen`` record how much of the plane the
    sweep actually enumerated, and ``skipped`` counts records left
    untouched by reason (``suspended``, ``idle_fresh``, ``creating``,
    ``running``, ``terminal``, ``bound_live``, ``creating_inflight``) —
    the counters that make "the reaper ran but saw nothing" observable
    instead of silently identical to "the reaper never ran".
    """
    lifecycle = lifecycle_config()
    if idle_timeout_s is None:
        idle_timeout_s = lifecycle.idle_timeout_s
    if create_grace_s is None:
        create_grace_s = lifecycle.create_grace_s
    if run_grace_s is None:
        run_grace_s = lifecycle.run_stale_s
    actions: list[ReapAction] = []
    skipped: dict[str, int] | None = None
    if stats is not None:
        stats["records_seen"] = 0
        stats["handles_seen"] = 0
        skipped = stats.setdefault("skipped", {})

    def skip(reason: str) -> None:
        if skipped is not None:
            skipped[reason] = skipped.get(reason, 0) + 1

    def emit(
        kind: str,
        session_id: str | None,
        sandbox_id: str | None,
        account_id: str | None = None,
    ) -> None:
        action = ReapAction(kind, session_id, sandbox_id, account_id)
        actions.append(action)
        if on_action is not None:
            # Isolate the callback — a remote throw (lease release or
            # run-ledger settle) OR a call that never returns both record
            # a failure and let the sweep continue. Appended directly —
            # never re-invoke on_action or it can fail the same way
            # forever.
            delivered = _bounded_call(lambda: on_action(action) or True, _ON_ACTION_BOUND_S)
            if delivered is None:
                actions.append(ReapAction("reap_error", session_id, sandbox_id))

    def persist(rec: SessionRecord) -> bool:
        """``store.put`` that cannot kill the sweep — the record stays
        non-terminal and the next sweep retries it. Bounded too: a
        stalled Dict write must not wedge the tick (SOR-271 round-4)."""
        wrote = _bounded_call(lambda: store.put(rec) or True, _PUT_BOUND_S)
        if wrote is None:
            emit("reap_error", rec.id, rec.sandbox_id)
            return False
        return True

    # Index-doc self-heal: manifest RMW writes can lose entries across
    # containers, orphaning records from ``list_all`` — an agent invisible
    # to the listing is invisible to this sweep and leaks its live-agent
    # slot forever (the SOR-268 zombie finding). Rebuild once per sweep
    # (a keys() enumeration on the cron, never on a request path).
    rebuild_index = getattr(store, "rebuild_index", None)
    if callable(rebuild_index):
        # ``keys()`` enumeration is a serial remote walk — bound it so a
        # stalled Dict RPC cannot consume the whole tick before records.
        _bounded_call(lambda: rebuild_index() or True, _REBUILD_BOUND_S)

    sweep_deadline = time.monotonic() + deadline_s if deadline_s is not None else None

    def deadline_hit() -> bool:
        return sweep_deadline is not None and time.monotonic() > sweep_deadline

    if account_registry is not None:
        listed = _bounded_call(lambda: list(account_registry.list()), _REGISTRY_BOUND_S)
        if listed is None:
            accounts = []
            emit("reap_error", None, None)
        else:
            accounts = listed
        for acct in accounts:
            if deadline_hit():
                emit("sweep_deadline", None, None)
                return actions
            if not cooldown_expired(acct, now):
                continue
            marked = _bounded_call(
                lambda: account_registry.mark_status(acct.id, "active") or True,
                _REGISTRY_BOUND_S,
            )
            if marked is None:
                # Missing — or a stored record whose id fails account_id
                # validation (SOR-105): never usable, leave it — or a
                # wedged remote write; either way retry next sweep.
                continue
            emit("account_recovered", None, None, account_id=acct.id)

    records_all = _bounded_call(lambda: _list_all(store), _LIST_BOUND_S)
    if records_all is None:
        # No listing, no sweep — surface the failure and leave the rest
        # (the orphan pass below) to run on what it can enumerate.
        records_all = []
        emit("reap_error", None, None)
    if on_scan is not None:
        on_scan(records_all)
    if stats is not None:
        stats["records_seen"] = len(records_all)
    for rec in records_all:
        if deadline_hit():
            emit("sweep_deadline", None, None)
            return actions
        if rec.status in TERMINAL_STATUSES:
            skip("terminal")
            continue
        if rec.status == "suspended":
            # SOR-180: checkpointed and released — owns no live sandbox;
            # a follow-up message restores it via the checkpoint service.
            skip("suspended")
            continue
        if rec.status == "creating" and rec.sandbox_tags.get("lifecycle_claim"):
            # A VPS restart can interrupt capture after the checkpoint landed
            # but before the released state was persisted. No turn can dispatch
            # under this claim, so the durable checkpoint safely wins.
            ready = _bounded_call(lambda: checkpoints.has_checkpoint(rec.id), _SUSPEND_BOUND_S)
            if ready:
                handle = rec.handle()
                if handle is not None:
                    _bounded_call(lambda: backend.terminate(handle) or True, _TERMINATE_BOUND_S)
                rec.status = "suspended"
                rec.sandbox_tags.pop("lifecycle_claim", None)
                rec.updated_at = now
                if persist(rec):
                    emit("suspended", rec.id, rec.sandbox_id)
                continue
        if rec.status == "creating" and not rec.sandbox_id:
            # Record published before the sandbox bound (SOR-80 create order).
            basis = (
                rec.updated_at if rec.sandbox_tags.get("recovering") == "true" else rec.created_at
            )
            age_s = (now - basis).total_seconds()
            if age_s < create_grace_s:
                skip("creating")
                continue
            rec.status = "lost"
            rec.ended_at = now
            rec.updated_at = now
            if persist(rec):
                emit("lost", rec.id, None)
            continue
        handle = rec.handle()
        poll = (
            _bounded_call(lambda: backend.poll(handle), _POLL_BOUND_S)
            if handle is not None
            else None
        )
        if handle is not None and poll is None:
            # A wedged poll means UNKNOWN, not dead — skip this record.
            # Before this guard a single throwing poll aborted the whole
            # sweep every cron tick, so records after it were never
            # reaped (SOR-268 zombie idle agents); the same applies to a
            # poll that never returns (SOR-271 round-4).
            emit("reap_error", rec.id, rec.sandbox_id)
            continue
        alive = bool(poll and poll.alive)
        last = rec.last_activity_at or rec.updated_at
        idle_expired = (now - last).total_seconds() >= idle_timeout_s

        if not alive:
            if rec.status == "idle" and checkpoints is not None:
                # ``None`` (exception OR timeout) means UNKNOWN checkpoint
                # state — never misdiagnose as platform loss; skip and
                # retry next sweep.
                has_checkpoint = _bounded_call(
                    lambda: bool(checkpoints.has_checkpoint(rec.id)),
                    _SUSPEND_BOUND_S,
                )
                if has_checkpoint is None:
                    emit("reap_error", rec.id, rec.sandbox_id)
                    continue
                if has_checkpoint:
                    # A checkpoint landed but the suspend transition did
                    # not (half-finished sweep) — heal to the recoverable
                    # suspended state instead of losing the agent.
                    rec.status = "suspended"
                    rec.updated_at = now
                    if persist(rec):
                        emit("suspended", rec.id, rec.sandbox_id)
                    continue
                # SOR-180: explicit diagnosis — the platform reclaimed an
                # idle agent before any checkpoint could be taken; the
                # agent's filesystem and native session are unrecoverable.
                rec.status = "lost"
                rec.ended_at = now
                rec.updated_at = now
                rec.current_turn_id = None
                rec.current_turn_n = None
                persisted = persist(rec)
                _bounded_call(
                    lambda: (
                        checkpoints.fail(
                            rec.id,
                            "platform loss before checkpoint: "
                            f"sandbox {rec.sandbox_id} gone, no usable snapshot",
                        )
                        or True
                    ),
                    _REGISTRY_BOUND_S,
                )
                if persisted:
                    emit("platform_loss", rec.id, rec.sandbox_id)
                continue
            rec.status = "timed_out" if rec.status == "idle" else "lost"
            rec.ended_at = now
            rec.updated_at = now
            rec.current_turn_id = None
            rec.current_turn_n = None
            if persist(rec):
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
                if persist(rec):
                    emit("lost", rec.id, rec.sandbox_id)
            else:
                skip("creating")
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
                if persist(rec):
                    emit("lost", rec.id, rec.sandbox_id)
                if handle is not None:
                    terminated = _bounded_call(
                        lambda: backend.terminate(handle) or True, _TERMINATE_BOUND_S
                    )
                    if terminated is None:
                        emit("cleanup_failed", rec.id, rec.sandbox_id)
            else:
                skip("running")
            continue

        if rec.status == "idle" and idle_expired and handle is not None:
            suspended = False
            if checkpoints is not None:
                # Bounded: an exception OR a checkpoint that never returns
                # both fall through to terminate + ``timed_out``.
                suspended = bool(
                    _bounded_call(lambda: checkpoints.suspend(rec, handle), _SUSPEND_BOUND_S)
                )
            if suspended:
                # SOR-180: filesystem checkpointed (credentials scrubbed
                # first) → release the sandbox → recoverable ``suspended``
                # rather than terminal ``timed_out``. A failed terminate
                # leaves a sandbox the orphan pass reclaims next sweep.
                terminated = _bounded_call(
                    lambda: backend.terminate(handle) or True, _TERMINATE_BOUND_S
                )
                if terminated is None:
                    emit("cleanup_failed", rec.id, rec.sandbox_id)
                rec.status = "suspended"
                rec.updated_at = now
                if persist(rec):
                    emit("suspended", rec.id, rec.sandbox_id)
                continue
            terminated = _bounded_call(
                lambda: backend.terminate(handle) or True, _TERMINATE_BOUND_S
            )
            if terminated is None:
                emit("cleanup_failed", rec.id, rec.sandbox_id)
            rec.status = "timed_out"
            rec.ended_at = now
            rec.updated_at = now
            rec.current_turn_id = None
            rec.current_turn_n = None
            if persist(rec):
                emit("timed_out", rec.id, rec.sandbox_id)
            continue

        # Everything that fell through keeps no action this tick: ``idle``
        # inside its retention window, or a status this sweep does not own.
        if rec.status == "idle" and not idle_expired:
            skip("idle_fresh")
        else:
            skip("unhandled")

    listed = _bounded_call(lambda: _list_all(store), _LIST_BOUND_S)
    if listed is None:
        emit("reap_error", None, None)
        return actions
    records = {rec.id: rec for rec in listed}
    bound_live = {
        rec.sandbox_id
        for rec in records.values()
        if rec.sandbox_id
        and rec.status not in TERMINAL_STATUSES
        # A suspended record keeps its released sandbox_id as provenance —
        # it owns no live sandbox, so a surviving handle is an orphan.
        and rec.status != "suspended"
    }
    handles = _bounded_call(backend.list, _LIST_BOUND_S)
    if handles is None:
        emit("reap_error", None, None)
        return actions
    if stats is not None:
        stats["handles_seen"] = len(handles)
    for handle in handles:
        if deadline_hit():
            emit("sweep_deadline", None, None)
            return actions
        if handle.id in bound_live:
            skip("bound_live")
            continue
        rec = _session_record(records, handle)
        if rec is not None and rec.status not in TERMINAL_STATUSES:
            # Same-session record without a bound sandbox id → in-flight
            # create in the record-publication window; never orphan-kill it.
            if rec.sandbox_id is None:
                skip("creating_inflight")
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
        terminated = _bounded_call(lambda: backend.terminate(handle) or True, _TERMINATE_BOUND_S)
        if terminated is None:
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


def sweep_plane(
    plane: Any,
    *,
    v1_state: Any = None,
    account_registry: AccountRegistry | None = None,
    now: datetime | None = None,
    reconcile_bound_s: float = _RECONCILE_BOUND_S,
    sweep_bound_s: float = _SWEEP_BOUND_S,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """One full reaper tick for a plane — the production cron's body.

    Two phases, each *time*-bounded rather than merely exception-guarded
    (SOR-271 round-4): a remote call that throws degrades to a skipped
    stage, and so does one that never returns — the pre-fix wedge that
    let a firing cron produce zero observable reaping for days.

    1. ``plane.reconcile_turns()`` settles watcher-less ``running``
       records from written turn evidence (so a provider success lands
       FINISHED + idle instead of ``lost``). Bounded — on timeout the
       leftover daemon thread finishes in the background and the tick
       proceeds to the reaper regardless.
    2. ``reap()`` under ``deadline_s`` (the remainder of
       ``sweep_bound_s``), with ``on_action`` wiring ``/v1`` lease release
       + orphaned-run settlement — the same wiring the cron carried,
       moved here so the whole tick is exercised in unit tests.

    ``log`` (default ``print`` when called with ``log=print`` — pass a
    collector in tests) receives one line per phase boundary so the
    deployed function's stdout proves the tick ran and what it did.
    Returns a summary dict: ``actions`` (the emitted ``ReapAction``s),
    ``action_kinds``, ``settled_turns``, ``elapsed_s``, ``now``.
    """
    from control.service import release_lease_for_action

    emit_log = log if log is not None else (lambda _m: None)
    started = time.monotonic()
    now = now or datetime.now(UTC)
    lifecycle = lifecycle_config()
    emit_log(f"[reap] tick start at={now.isoformat()} bound_s={sweep_bound_s}")

    reconcile = getattr(plane, "reconcile_turns", None)
    settled = _bounded_call(lambda: reconcile(), reconcile_bound_s) if callable(reconcile) else []
    if settled is None:
        emit_log(
            f"[reap] reconcile exceeded {reconcile_bound_s}s — proceeding "
            "to sweep; a hung remote read must never starve the reaper"
        )
        settled = []
    else:
        emit_log(f"[reap] reconcile settled {len(settled)} turn(s)")

    settle = getattr(plane, "settle_orphaned_runs", None)

    def _on_action(action: ReapAction) -> None:
        # A ``suspended`` agent owns no live sandbox — its lease must free
        # like every other reaped state: an unreleased slot wedges new
        # binds at the global cap even though the agent holds nothing
        # (SOR-271 round-5). Recoverability lives in the durable
        # checkpoint, not in the capacity slot. Kinds that carry no
        # session (``reap_error``, ``sweep_deadline``,
        # ``account_recovered``) no-op through ``release_lease``.
        release_lease_for_action(v1_state, action)
        if (
            action.kind in ("lost", "timed_out", "platform_loss")
            and action.session_id
            and callable(settle)
        ):
            # The session just went terminal — persist a terminal verdict
            # for any still-open run so no record dangles RUNNING.
            settle(
                action.session_id,
                session_status="lost" if action.kind == "platform_loss" else action.kind,
            )

    remaining = sweep_bound_s - (time.monotonic() - started)
    scan: dict[str, Any] = {
        "store": getattr(plane.store, "_name", type(plane.store).__name__),
        "backend": type(plane.backend).__name__,
        "idle_timeout_s": lifecycle.idle_timeout_s,
        "records": 0,
        "by_status": {},
        "expired_idle": 0,
    }

    def _on_scan(records: list[SessionRecord]) -> None:
        scan["records"] = len(records)
        for record in records:
            counts = scan["by_status"]
            counts[record.status] = counts.get(record.status, 0) + 1
            last = record.last_activity_at or record.updated_at
            if record.status == "idle" and (now - last).total_seconds() >= lifecycle.idle_timeout_s:
                scan["expired_idle"] += 1
        emit_log(f"[reap] scan {scan}")

    stats: dict[str, Any] = {"records_seen": 0, "handles_seen": 0, "skipped": {}}
    actions = reap(
        plane.store,
        plane.backend,
        now,
        idle_timeout_s=lifecycle.idle_timeout_s,
        create_grace_s=lifecycle.create_grace_s,
        run_grace_s=lifecycle.run_stale_s,
        account_registry=account_registry,
        checkpoints=getattr(plane, "checkpoints", None),
        on_action=_on_action,
        on_scan=_on_scan,
        deadline_s=max(1.0, remaining),
        stats=stats,
    )
    kinds: dict[str, int] = {}
    for action in actions:
        kinds[action.kind] = kinds.get(action.kind, 0) + 1
    elapsed = time.monotonic() - started
    emit_log(
        f"[reap] tick done elapsed_s={elapsed:.1f} actions={len(actions)} kinds={kinds} "
        f"records_seen={stats['records_seen']} handles_seen={stats['handles_seen']} "
        f"skipped={stats['skipped']}"
    )
    return {
        "actions": actions,
        "action_kinds": kinds,
        "settled_turns": settled,
        "elapsed_s": elapsed,
        "now": now,
        "scan": scan,
        "records_seen": stats["records_seen"],
        "handles_seen": stats["handles_seen"],
        "skipped": stats["skipped"],
    }

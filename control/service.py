"""Session state machine and sandbox orchestration."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from control.backend import Process, SandboxBackend, SandboxHandle, SandboxSpec
from control.config import (
    DEFAULT_MODEL,
    IDLE_TIMEOUT_S,
    MAX_CONCURRENT,
    SANDBOX_USD_PER_S,
    TERMINAL_STATUSES,
    TURN_MAX_SECONDS,
)
from control.run_errors import run_error_for_run
from control.run_store import (
    RunLedger,
    apply_output_contract,
    outcome_from_turn_payload,
    run_error,
)
from control.sandbox_io import drain, read_json, sandbox_env, write_file
from control.store import SessionRecord, SessionStore, merge_usage

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def iso(ts: datetime) -> str:
    return ts.isoformat()


def cost_estimate_usd(sandbox_seconds: float) -> float:
    return round(max(0.0, sandbox_seconds) * SANDBOX_USD_PER_S, 6)


@dataclass
class LiveTurn:
    turn_id: str
    n: int
    proc: Process


class SessionConflict(Exception):
    def __init__(self, error: str, code: int = 409) -> None:
        super().__init__(error)
        self.error = error
        self.code = code


class ConcurrencyLimit(Exception):
    def __init__(self) -> None:
        super().__init__("concurrency_limit")
        self.error = "concurrency_limit"
        self.code = 429


def release_lease(v1_state: Any, session_id: str | None) -> None:
    """Idempotently release a ``/v1`` scheduler lease held for ``session_id``.

    The lease surface lives on ``app.state.v1_state`` (owned by api_v1); the
    control plane coordinates through ``pop_lease`` + ``release()`` — the same
    calls ``/v1`` uses — without importing the routes module.
    """
    if v1_state is None or not session_id:
        return
    pop = getattr(v1_state, "pop_lease", None)
    if not callable(pop):
        return
    lease = pop(session_id)
    release = getattr(lease, "release", None)
    if callable(release):
        release()


def release_lease_for_action(v1_state: Any, action: Any) -> None:
    """Release the ``/v1`` lease for a reaper action's session, if any."""
    release_lease(v1_state, getattr(action, "session_id", None))


class ControlPlane:
    def __init__(
        self,
        backend: SandboxBackend,
        store: SessionStore,
        runner_cmd: list[str],
        *,
        clock: Clock | None = None,
        max_concurrent: int = MAX_CONCURRENT,
        default_model: str = DEFAULT_MODEL,
        idle_timeout_s: int = IDLE_TIMEOUT_S,
        turn_max_seconds: int = TURN_MAX_SECONDS,
        run_ledger: RunLedger | None = None,
        workspaces: Any = None,
        handoffs: Any = None,
    ) -> None:
        self.backend = backend
        self.store = store
        self.runner_cmd = list(runner_cmd)
        self.clock = clock or _utcnow
        self.max_concurrent = max_concurrent
        self.default_model = default_model
        self.idle_timeout_s = idle_timeout_s
        self.turn_max_seconds = turn_max_seconds
        self.run_ledger = run_ledger
        # SOR-83: optional WorkspaceService / HandoffService wired by the app
        # layer; ``None`` means workspace declarations are not configured.
        self.workspaces = workspaces
        self.handoffs = handoffs
        # SOR-83: fired inside close() after runs are finalized and before the
        # sandbox is terminated, so workspace artifacts land in the durable
        # store while the sandbox is still readable. Best-effort: failures are
        # swallowed — a broken snapshot must never wedge teardown.
        self.snapshot_hook: Callable[[SessionRecord, SandboxHandle], None] | None = None
        # SOR-127 environment build/snapshot cache — optional, wired by the
        # app layer. ``snapshot_provider`` restores a sandbox from a build
        # record's snapshot ref; ``environments`` is the build-record
        # service the /v1 worker resolves/fills through. Both None means
        # the cache is disabled and provisioning is unchanged.
        self.snapshot_provider: Any = None
        self.environments: Any = None
        self._lock = threading.RLock()
        self._live: dict[str, LiveTurn] = {}
        # Per-session provider/account/model context for run records. Lost on
        # restart; persisted RunRecords carry their own copies, and sandbox
        # tags keep provider/account for sessions that predate the restart.
        self._run_meta: dict[str, dict[str, str | None]] = {}
        # Sessions whose first turn was queued by ``open_session`` but not yet
        # dispatched: post_message must not allocate that turn id to a
        # follow-up run in the gap between provision and dispatch (SOR-82 A2).
        self._first_turn_pending: set[str] = set()

    def runner(self, *args: str) -> list[str]:
        return [*self.runner_cmd, *args]

    def public(self, rec: SessionRecord) -> dict[str, Any]:
        now = self.clock()
        if rec.status in TERMINAL_STATUSES:
            end = rec.ended_at or rec.updated_at or now
        else:
            end = now
        sandbox_seconds = max(0.0, (end - rec.created_at).total_seconds())
        usage = dict(rec.usage or {})
        usage.setdefault("input_tokens", 0)
        usage.setdefault("cached_input_tokens", 0)
        usage.setdefault("output_tokens", 0)
        return {
            "id": rec.id,
            "title": rec.title,
            "status": rec.status,
            "created_at": iso(rec.created_at),
            "updated_at": iso(rec.updated_at),
            "model": rec.model,
            "turns": rec.turns,
            "usage": usage,
            "cost_estimate_usd": cost_estimate_usd(sandbox_seconds),
            "sandbox_seconds": sandbox_seconds,
            "messages": list(rec.messages),
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        return [self.public(rec) for rec in self.store.list_all()]

    def get(self, session_id: str) -> SessionRecord | None:
        return self.store.get(session_id)

    def find_by_idempotency(self, owner: str, key: str) -> SessionRecord | None:
        """Durable ``Idempotency-Key`` lookup across restarts (SOR-82).

        The in-memory ``IdempotencyStore`` only covers one process; the
        session record pins ``(owner, key)`` durably so a retry landing after
        a control-plane restart still resolves to the original agent.
        ``lost`` records are failed creates — the key is free for retry.
        """
        with self._lock:
            for rec in self.store.list_all():
                if rec.owner == owner and rec.idempotency_key == key and rec.status != "lost":
                    return rec
        return None

    def create_session(
        self,
        *,
        owner: str,
        title: str | None,
        model: str | None,
        provider: str = "codex",
        account_id: str = "auto",
        secret_name: str | None = None,
    ) -> str:
        session_id = self.open_session(
            owner=owner,
            title=title,
            model=model,
            provider=provider,
            account_id=account_id,
        )
        self.provision_session(
            session_id,
            provider=provider,
            account_id=account_id,
            secret_name=secret_name,
        )
        return session_id

    def open_session(
        self,
        *,
        owner: str,
        title: str | None,
        model: str | None,
        provider: str = "codex",
        account_id: str = "auto",
        first_prompt: str | None = None,
        idempotency_key: str | None = None,
        idempotency_fingerprint: str | None = None,
        output_contract: dict[str, Any] | None = None,
        resource_refs: dict[str, Any] | None = None,
    ) -> str:
        """Publish a ``creating`` record without provisioning the sandbox.

        SOR-82 A2: ``/v1`` callers return immediately after this call and run
        ``provision_session`` on a background thread; ``/api`` keeps calling
        ``create_session``, which is ``open_session`` + ``provision_session``
        inline and unchanged.

        ``first_prompt`` queues the first run: its user message is appended as
        ``turn-1``, the turn id is reserved in ``_first_turn_pending`` so a
        follow-up ``post_message`` cannot steal it before the worker
        dispatches, and a ``CREATING`` run-1 record is persisted in the run
        ledger before the session record becomes visible.

        ``idempotency_key``/``idempotency_fingerprint`` pin a client-supplied
        ``Idempotency-Key`` to the durable record so a retry that lands after
        a control-plane restart still dedups (``find_by_idempotency``).
        """
        with self._lock:
            live = self.backend.list(tags={"owner": owner})
            # In-flight creates own no sandbox yet — count them against the cap
            # so N parallel creates cannot overshoot it. A ``creating`` record
            # whose sandbox already exists (bind pending) is counted via
            # ``live`` only — never twice.
            live_ids = {(h.tags or {}).get("session_id") for h in live}
            creating = sum(
                1
                for rec in self.store.list_all()
                if rec.owner == owner and rec.status == "creating" and rec.id not in live_ids
            )
            if len(live) + creating >= self.max_concurrent:
                raise ConcurrencyLimit()
            session_id = uuid.uuid4().hex
            tags = {"session_id": session_id, "owner": owner}
            # Preserve the exact P1 sandbox shape for legacy /api callers.
            # P2.1 provider sessions carry enough metadata for Modal to select
            # the correct image and reattach the per-account Secret on exec.
            if provider != "codex" or account_id != "auto":
                tags.update({"provider": provider, "account_id": account_id})
            if resource_refs:
                # SOR-129: declared resource *refs* (names only — never
                # values) ride the durable sandbox tags so the agent view
                # stays truthful across control-plane restarts.
                tags["resources"] = json.dumps(resource_refs)
            now = self.clock()
            messages: list[dict[str, Any]] = []
            if first_prompt is not None:
                messages.append(
                    {"role": "user", "text": first_prompt, "turn_id": "turn-1", "ts": iso(now)}
                )
                self._first_turn_pending.add(session_id)
            rec = SessionRecord(
                id=session_id,
                title=title or "untitled",
                status="creating",
                created_at=now,
                updated_at=now,
                model=model or self.default_model,
                turns=0,
                usage=None,
                messages=messages,
                owner=owner,
                sandbox_tags=tags,
                last_activity_at=now,
                idempotency_key=idempotency_key,
                idempotency_fingerprint=idempotency_fingerprint,
            )
            self._run_meta[session_id] = {
                "provider": provider,
                "account_id": account_id,
                "model": rec.model,
            }
            if first_prompt is not None and self.run_ledger is not None:
                # The durable ledger is the source of truth: run-1 exists as
                # CREATING before the session record is visible, so no reader
                # can observe a queued run with no persisted state.
                self.run_ledger.begin(
                    agent_id=session_id,
                    n=1,
                    provider=provider,
                    account_id=account_id,
                    model=rec.model,
                    status="CREATING",
                    output_contract=output_contract,
                )
            # Publish the record before the sandbox exists: the reaper
            # distinguishes an in-flight create from an orphan sandbox via
            # this record (SOR-80), so ``backend.create`` cannot race it.
            self.store.put(rec)
            return session_id

    def provision_session(
        self,
        session_id: str,
        *,
        provider: str = "codex",
        account_id: str = "auto",
        secret_name: str | None = None,
        resource_secrets: list[str] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        env_snapshot: str | None = None,
    ) -> None:
        """Create the sandbox and run ``runner init`` for an open session.

        Blocking — intended for a worker thread under ``/v1`` (SOR-82 A2).
        On failure the record is terminal ``lost`` with sandbox metadata kept
        for the reaper; ``SessionConflict`` means the session was closed while
        provisioning ran.

        SOR-129 session resources: ``resource_secrets`` are Modal Secret
        names attached to this sandbox only (validated upstream — never
        account credential Secrets); ``mcp_servers`` are the resolved MCP
        config templates forwarded to ``runner init`` via
        ``SBX_MCP_SERVERS`` (env indirection, never values).

        SOR-127: ``env_snapshot`` is a prepared-environment snapshot ref —
        the sandbox is restored from it through ``snapshot_provider``
        instead of a cold ``backend.create``. Restoring still mounts this
        session's own Secrets (account credential channels are per-sandbox
        env, never filesystem state), and the restored base still has to
        pass the workspace ``base_sha`` gate at prepare time.
        """
        with self._lock:
            rec = self.store.get(session_id)
            if rec is None:
                raise KeyError(session_id)
            if rec.status != "creating":
                raise SessionConflict("session_not_runnable")
            tags = dict(rec.sandbox_tags)
        secrets = [secret_name] if secret_name else []
        spec = SandboxSpec(
            tags=tags,
            secrets=secrets,
            resource_secrets=list(resource_secrets or ()),
        )
        try:
            if env_snapshot is not None:
                if self.snapshot_provider is None:
                    raise RuntimeError(
                        "env_snapshot restore requested but no snapshot provider is configured"
                    )
                handle = self.snapshot_provider.restore(env_snapshot, spec)
            else:
                handle = self.backend.create(spec)
        except Exception:
            self._mark_create_failed(rec)
            raise

        with self._lock:
            stored = self.store.get(session_id)
            if stored is None or stored.status in TERMINAL_STATUSES:
                # Closed while the sandbox was being created — do not bind it.
                try:
                    self.backend.terminate(handle)
                except Exception:
                    pass
                raise SessionConflict("session_not_runnable")
            stored.sandbox_id = handle.id
            stored.sandbox_root = str(handle.root)
            stored.updated_at = self.clock()
            self.store.put(stored)
            rec = stored

        try:
            init_args = ["init", "--auth", "auth_json", "--model", rec.model]
            init_env: dict[str, str] = {}
            if provider != "codex" or account_id != "auto":
                init_args += ["--provider", provider]
                if account_id != "auto":
                    init_args += ["--account-id", account_id]
                    init_env["SBX_ACCOUNT_ID"] = account_id
            if mcp_servers:
                # SOR-129: resolved MCP config templates (${env:VAR}
                # indirection only — no secret values) for ``runner init``.
                init_env["SBX_MCP_SERVERS"] = json.dumps(list(mcp_servers))
            init = self.backend.exec(
                handle,
                self.runner(*init_args),
                env=sandbox_env(handle, init_env),
            )
            code = drain(init)
            if code != 0:
                tail = ""
                stderr_text = getattr(init, "stderr_text", None)
                if callable(stderr_text):
                    try:
                        tail = stderr_text().strip()
                    except Exception:
                        tail = ""
                detail = f": {tail[-300:]}" if tail else ""
                raise RuntimeError(f"runner init exited {code}{detail}")
        except Exception:
            # Mark the record lost *before* terminating: even if terminate
            # fails, the record stays terminal with sandbox_id bound so the
            # reaper can retry cleanup (SOR-80).
            self._mark_create_failed(rec)
            try:
                self.backend.terminate(handle)
            except Exception:
                pass
            raise

        with self._lock:
            stored = self.store.get(session_id)
            if stored is None or stored.status in TERMINAL_STATUSES:
                # Closed concurrently while init ran — do not resurrect it.
                try:
                    self.backend.terminate(handle)
                except Exception:
                    pass
                raise SessionConflict("session_not_runnable")
            stored.status = "idle"
            now = self.clock()
            stored.updated_at = now
            stored.last_activity_at = now
            self.store.put(stored)

    def _mark_create_failed(self, rec: SessionRecord) -> None:
        """Terminal ``lost`` transition for a failed create; keeps sandbox_id."""
        with self._lock:
            self._first_turn_pending.discard(rec.id)
            stored = self.store.get(rec.id) or rec
            if stored.status in TERMINAL_STATUSES:
                return
            stored.status = "lost"
            stored.ended_at = self.clock()
            stored.updated_at = stored.ended_at
            self.store.put(stored)

    def discard_queued_first_turn(self, session_id: str) -> None:
        """Drop a queued-but-undispatched first turn reservation (cancel path).

        Only the reservation marker is dropped — the session status is left
        alone. While provisioning is still in flight the record stays
        ``creating`` (the truth); the worker settles it to ``idle`` or
        ``lost`` when ``provision_session`` resolves. Reporting ``idle``
        early would let a follow-up run dispatch into a half-provisioned
        sandbox whose ``runner init`` is still blocked (SOR-82 review).
        """
        with self._lock:
            self._first_turn_pending.discard(session_id)

    def _next_turn_n(self, rec: SessionRecord) -> int:
        """Next never-reused turn number for ``rec``.

        ``turns`` counts finished turns only; queued-but-undispatched run
        messages (SOR-82 A2 ``first_prompt``) already occupy their turn id, so
        allocation is ``max(turns, message turn ids, current_turn_n) + 1``.
        """
        known = {rec.turns, rec.current_turn_n or 0}
        for message in rec.messages:
            turn_id = message.get("turn_id")
            if isinstance(turn_id, str) and turn_id.startswith("turn-"):
                try:
                    known.add(int(turn_id[5:]))
                except ValueError:
                    pass
        return max(known) + 1

    def post_message(
        self,
        session_id: str,
        text: str,
        *,
        output_contract: dict[str, Any] | None = None,
    ) -> str:
        # A ``running`` record with no in-process watcher is a stranded turn
        # (control-plane cutover); reconcile it from evidence first so a
        # finished provider run frees the agent instead of 409ing forever.
        self.reconcile_turn(session_id)
        with self._lock:
            rec = self.store.get(session_id)
            if rec is None:
                raise KeyError(session_id)
            if rec.status in TERMINAL_STATUSES:
                raise SessionConflict("session_not_runnable")
            if session_id in self._first_turn_pending:
                # The queued first run owns turn-1 until its worker dispatches.
                raise SessionConflict("turn_in_progress")
            if rec.status == "running" or rec.current_turn_id is not None:
                raise SessionConflict("turn_in_progress")
            if rec.status != "idle":
                # ``creating`` (or anything else non-idle) is not runnable.
                raise SessionConflict("session_not_runnable")
            handle = rec.handle()
            if handle is None:
                raise SessionConflict("session_not_runnable")
            n = self._next_turn_n(rec)
            turn_id = f"turn-{n}"
            now = self.clock()
            rec.status = "running"
            rec.current_turn_id = turn_id
            rec.current_turn_n = n
            rec.updated_at = now
            rec.messages.append({"role": "user", "text": text, "turn_id": turn_id, "ts": iso(now)})
            self.store.put(rec)
            if self.run_ledger is not None:
                meta = self._run_meta.get(session_id, {})
                self.run_ledger.begin(
                    agent_id=session_id,
                    n=n,
                    provider=meta.get("provider") or rec.sandbox_tags.get("provider") or "codex",
                    account_id=meta.get("account_id")
                    or rec.sandbox_tags.get("account_id")
                    or "auto",
                    model=meta.get("model") or rec.model,
                    output_contract=output_contract,
                )
        try:
            return self._dispatch_turn(session_id, turn_id, n, handle, text, drop_message=True)
        except Exception:
            # Rollback already finalized the record: a terminal (lost)
            # session means the sandbox died between the liveness check and
            # dispatch, so the refusal is the canonical session_not_runnable
            # rather than an unhandled 500.
            rec = self.store.get(session_id)
            if rec is not None and rec.status in TERMINAL_STATUSES:
                raise SessionConflict("session_not_runnable") from None
            raise

    def post_queued_first_turn(self, session_id: str) -> str:
        """Dispatch the run-1 queued by ``open_session(first_prompt=...)``.

        The user message was appended at open time; here we only flip the
        record to ``running`` and exec the turn. Raises SessionConflict when
        the queue marker is gone (already dispatched/dropped) or the session
        is not runnable.
        """
        with self._lock:
            rec = self.store.get(session_id)
            if rec is None:
                self._first_turn_pending.discard(session_id)
                raise KeyError(session_id)
            if session_id not in self._first_turn_pending:
                raise SessionConflict("turn_in_progress")
            if rec.status in TERMINAL_STATUSES:
                self._first_turn_pending.discard(session_id)
                raise SessionConflict("session_not_runnable")
            if rec.status != "idle":
                raise SessionConflict("session_not_runnable")
            handle = rec.handle()
            if handle is None:
                raise SessionConflict("session_not_runnable")
            self._first_turn_pending.discard(session_id)
            n = 1
            turn_id = f"turn-{n}"
            rec.status = "running"
            rec.current_turn_id = turn_id
            rec.current_turn_n = n
            rec.updated_at = self.clock()
            self.store.put(rec)
            if self.run_ledger is not None:
                # The run-1 record already exists (CREATING, written by
                # open_session / the route seam); dispatch is the RUNNING
                # transition. begin() backstops records created before the
                # ledger existed; mark_running is terminal-safe.
                meta = self._run_meta.get(session_id, {})
                self.run_ledger.begin(
                    agent_id=session_id,
                    n=n,
                    provider=meta.get("provider") or rec.sandbox_tags.get("provider") or "codex",
                    account_id=meta.get("account_id")
                    or rec.sandbox_tags.get("account_id")
                    or "auto",
                    model=meta.get("model") or rec.model,
                )
                self.run_ledger.mark_running(session_id, n)
        text = next(
            (
                str(m.get("text") or "")
                for m in rec.messages
                if m.get("turn_id") == turn_id and m.get("role") == "user"
            ),
            "",
        )
        # On dispatch failure the queued user message stays: turn-1 remains
        # allocated to run-1 (which the caller marks ERROR) and the next
        # post_message allocates turn-2 — run ids never collide.
        return self._dispatch_turn(session_id, turn_id, n, handle, text, drop_message=False)

    def _dispatch_turn(
        self,
        session_id: str,
        turn_id: str,
        n: int,
        handle: Any,
        text: str,
        *,
        drop_message: bool,
    ) -> str:
        rel = f"_prompt_{n}.md"
        # SOR-130: the run's normalized output contract (persisted on the
        # ledger record at begin) rides into the sandbox as _contract_<n>.json
        # so the runner can steer + evaluate the provider's final message.
        contract = None
        if self.run_ledger is not None:
            record = self.run_ledger.get(session_id, n)
            contract = record.output_contract if record is not None else None
        turn_args = [
            "turn",
            "--n",
            str(n),
            "--message-file",
            str(handle.root / rel),
            "--max-seconds",
            str(self.turn_max_seconds),
        ]
        try:
            write_file(self.backend, handle, rel, text)
            if contract is not None:
                contract_rel = f"_contract_{n}.json"
                write_file(
                    self.backend,
                    handle,
                    contract_rel,
                    json.dumps(
                        {
                            "schema": contract.get("schema"),
                            "enforcement": contract.get("enforcement", "strict"),
                        }
                    ),
                )
                turn_args += ["--output-contract", str(handle.root / contract_rel)]
            proc = self.backend.exec(
                handle,
                self.runner(*turn_args),
                env=sandbox_env(handle),
            )
        except Exception:
            self._rollback_turn(session_id, turn_id, handle, drop_message=drop_message)
            raise
        with self._lock:
            self._live[session_id] = LiveTurn(turn_id=turn_id, n=n, proc=proc)
        thread = threading.Thread(
            target=self._watch_turn,
            args=(session_id, turn_id, n, proc),
            daemon=True,
            name=f"sbx-turn-{session_id}-{n}",
        )
        thread.start()
        return turn_id

    def _rollback_turn(
        self, session_id: str, turn_id: str, handle: Any, *, drop_message: bool = True
    ) -> None:
        """Undo a queued turn whose write/exec never started (SOR-80).

        Deterministic end state: ``idle`` when the sandbox is still alive
        (runnable again) or ``lost`` when it is gone (terminal). The pending
        user message is rolled back too, except for queued first turns
        (``drop_message=False``) whose turn id must stay allocated.
        """
        with self._lock:
            rec = self.store.get(session_id)
            if rec is None or rec.current_turn_id != turn_id:
                return
            try:
                alive = bool(self.backend.poll(handle).alive)
            except Exception:
                alive = False
            now = self.clock()
            if drop_message:
                rec.messages = [m for m in rec.messages if m.get("turn_id") != turn_id]
            rec.current_turn_id = None
            rec.current_turn_n = None
            rec.status = "idle" if alive else "lost"
            if not alive:
                rec.ended_at = now
            rec.updated_at = now
            rec.last_activity_at = now
            self.store.put(rec)
            if self.run_ledger is not None and drop_message:
                # The turn never started; its open run record is rolled back
                # with the pending user message. Queued first turns keep
                # theirs (drop_message=False): run-1 stays allocated so the
                # caller can persist the terminal startup failure on it.
                try:
                    n = int(turn_id.rsplit("-", 1)[-1])
                except ValueError:
                    n = 0
                if n:
                    self.run_ledger.discard(session_id, n)

    def _watch_turn(self, session_id: str, turn_id: str, n: int, proc: Process) -> None:
        try:
            drain(proc)
        except Exception:
            pass
        self._finish_turn(session_id, turn_id, n)

    def _finish_turn(self, session_id: str, turn_id: str, n: int) -> None:
        # Phase 1 (locked): snapshot the live handle only. Everything after
        # this — the backend evidence read and the contract verdict — runs
        # unlocked, so untrusted work can strand this watcher thread but
        # never the whole control plane (SOR-130 review).
        with self._lock:
            rec = self.store.get(session_id)
            handle = rec.handle() if rec is not None else None
        payload = None
        if handle is not None:
            try:
                payload = read_json(self.backend, handle, f"turns/{n}.json")
            except Exception:
                # Sandbox reclaimed mid-turn: the turn outcome is
                # unreadable — the ledger persist below records it as
                # ERROR, never success.
                payload = None
        # Phase 2 (unlocked): judge the evidence. apply_output_contract is
        # pure and budget-bounded; the backstop keeps even an unforeseen
        # failure diagnosable instead of wedging the run open.
        contract = None
        try:
            status, error, result_text, usage = outcome_from_turn_payload(payload)
            # SOR-130: enforce the run's output contract on the recorded
            # message — a strict violation becomes ERROR +
            # contract_violation, never a silent FINISHED.
            if self.run_ledger is not None:
                record = self.run_ledger.get(session_id, n)
                contract = record.output_contract if record is not None else None
            status, error, structured_output, contract_result = apply_output_contract(
                status, error, result_text, contract
            )
        except Exception:
            # The enforcement seam judges sandbox-written evidence, so it is
            # designed total — this guard is the last resort: a failure to
            # evaluate fails closed, the run still terminates diagnosably
            # instead of wedging open.
            status, result_text, usage, structured_output = (
                "ERROR",
                None,
                None,
                None,
            )
            error = run_error(
                "runtime_error",
                "turn outcome could not be evaluated",
                source="control",
                retryable=True,
            )
            contract_result = (
                {
                    "enforcement": str(contract.get("enforcement") or "strict"),
                    "schema_digest": contract.get("schema_digest"),
                    "status": "invalid",
                    "extraction": None,
                    "violations": [
                        {
                            "path": "$",
                            "code": "evaluation_error",
                            "message": "turn outcome could not be evaluated",
                        }
                    ],
                }
                if isinstance(contract, dict)
                else None
            )
        # Phase 3 (locked): fold the evidence into the session record and
        # persist the terminal outcome. finish() is monotonic, so a cancel
        # recorded by a concurrent stop()/close() still wins over this late
        # success — the verdict computed unlocked cannot resurrect a run.
        with self._lock:
            rec = self.store.get(session_id)
            active = rec is not None and rec.status not in TERMINAL_STATUSES
            now = self.clock()
            if active and payload is not None:
                # The turn payload is sandbox-written evidence: corrupt
                # fields degrade individually, they never wedge the finish.
                try:
                    turn_usage = payload.get("usage")
                    rec.usage = merge_usage(
                        rec.usage, turn_usage if isinstance(turn_usage, dict) else None
                    )
                except (TypeError, ValueError, AttributeError):
                    pass
                try:
                    rec.turns = max(rec.turns, int(payload.get("n") or n))
                except (TypeError, ValueError):
                    rec.turns = max(rec.turns, n)
                message = payload.get("message") or ""
                if message and rec.current_turn_id == turn_id:
                    rec.messages.append(
                        {
                            "role": "assistant",
                            "text": str(message),
                            "turn_id": turn_id,
                            "ts": iso(now),
                        }
                    )
            if active:
                if rec.current_turn_id == turn_id:
                    rec.current_turn_id = None
                    rec.current_turn_n = None
                if rec.status == "running":
                    rec.status = "idle"
                rec.updated_at = now
                rec.last_activity_at = now
                self.store.put(rec)
            if self.run_ledger is not None:
                # Persist the terminal outcome now, while turns/<n>.json may
                # still be readable; after teardown this record is the only
                # evidence.
                self.run_ledger.finish(
                    session_id,
                    n,
                    status=status,
                    result_text=result_text,
                    error=error,
                    usage=usage,
                    structured_output=structured_output,
                    contract_result=contract_result,
                )
            self._live.pop(session_id, None)

    def reconcile_turn(self, session_id: str) -> bool:
        """Settle a ``running`` record whose in-process watcher is gone (SOR-139).

        A control-plane restart or deploy cutover drains the container and its
        ``_watch_turn`` threads; the session record then stays ``running``
        forever — refusing follow-ups and publish — even when the provider
        already wrote ``turns/<n>.json``. The settle is evidence-gated: only a
        readable turn payload proves completion, so a turn still executing on
        the live sandbox (its watcher lives on the drained container, or it
        is genuinely wedged — the reaper's ``run_grace_s`` bound owns that
        case) is left alone. The fold is ``_finish_turn`` itself, so the
        reconciler persists identical session/ledger truth to the watcher's
        own persist and stays monotonic — a late watcher cannot rewrite a
        reconciled terminal.

        Returns ``True`` when the turn was settled from evidence.
        """
        with self._lock:
            if session_id in self._live:
                return False
            rec = self.store.get(session_id)
            if (
                rec is None
                or rec.status != "running"
                or rec.current_turn_n is None
                or rec.current_turn_id is None
            ):
                return False
            n = int(rec.current_turn_n)
            turn_id = rec.current_turn_id
            handle = rec.handle()
        if handle is None:
            return False
        try:
            if not self.backend.poll(handle).alive:
                # Dead sandbox: the reaper's lost/timed_out transition owns it.
                return False
            payload = read_json(self.backend, handle, f"turns/{n}.json")
        except Exception:
            return False
        if payload is None:
            return False
        self._finish_turn(session_id, turn_id, n)
        return True

    def reconcile_turns(self) -> list[str]:
        """Settle every watcher-less ``running`` session from turn evidence.

        A cron/reaper plane owns no watchers (``_live`` is per-process), so
        every ``running`` record is a candidate; only positive
        ``turns/<n>.json`` evidence finalizes. Run before the reaper so a
        provider success lands FINISHED + idle instead of ``lost`` when the
        container died mid-watch.
        """
        settled: list[str] = []
        for rec in self.store.list_all():
            if rec.status == "running" and self.reconcile_turn(rec.id):
                settled.append(rec.id)
        return settled

    def settle_orphaned_runs(self, session_id: str, *, session_status: str) -> list[int]:
        """Persist a terminal verdict for a terminal session's open runs.

        The reaper closes watcher-less sessions (``lost`` / ``timed_out``)
        whose runs never produced readable evidence; without this their
        ledger records stay open forever — read back as UNKNOWN, a perpetual
        non-answer. The session's terminal state is the durable truth: each
        still-open run is persisted to the matching terminal outcome — never
        FINISHED without evidence.
        """
        if self.run_ledger is None:
            return []
        if session_status in ("timed_out", "lost"):
            status = "EXPIRED"
            verdict = run_error_for_run("EXPIRED", agent_status=session_status)
        else:
            status = "ERROR"
            verdict = run_error_for_run("ERROR")
        error = verdict.public() if verdict is not None else None
        settled: list[int] = []
        for record in self.run_ledger.list(session_id):
            if record.terminal:
                continue
            self.run_ledger.finish(session_id, record.n, status=status, error=error)
            settled.append(record.n)
        return settled

    def stop(self, session_id: str) -> str:
        # Settle evidence first: a turn the provider already finished must not
        # be rewritten to CANCELLED by a stop landing after its watcher died.
        self.reconcile_turn(session_id)
        with self._lock:
            rec = self.store.get(session_id)
            if rec is None:
                raise KeyError(session_id)
            self._first_turn_pending.discard(session_id)
            handle = rec.handle()
            live = self._live.get(session_id)
            if rec.status == "running":
                if self.run_ledger is not None and rec.current_turn_n is not None:
                    # Persist CANCELLED before the session goes idle: a late
                    # _finish_turn can then never flip the run to FINISHED.
                    self.run_ledger.cancel(session_id, int(rec.current_turn_n))
                rec.status = "idle"
                rec.current_turn_id = None
                rec.current_turn_n = None
                rec.updated_at = self.clock()
                rec.last_activity_at = rec.updated_at
                self.store.put(rec)
        if live is not None:
            live.proc.kill()
        if handle is not None:
            try:
                stop = self.backend.exec(handle, self.runner("stop"), env=sandbox_env(handle))
                drain(stop)
            except Exception:
                # Best-effort hook only: the ledger cancel + proc kill above
                # are the real stop; a dead sandbox has nothing left to run
                # it on and must not mask an already-persisted cancel.
                pass
        with self._lock:
            self._live.pop(session_id, None)
            rec = self.store.get(session_id)
            return rec.status if rec else "closed"

    def close(self, session_id: str) -> SessionRecord:
        # Reconcile before cancelling open runs: a provider success must land
        # FINISHED, not CANCELLED, when the watcher died ahead of the close.
        self.reconcile_turn(session_id)
        with self._lock:
            rec = self.store.get(session_id)
            if rec is None:
                raise KeyError(session_id)
            self._first_turn_pending.discard(session_id)
            live = self._live.pop(session_id, None)
            handle = rec.handle()
            self._run_meta.pop(session_id, None)
            if self.run_ledger is not None:
                # Runs still open can never complete once the sandbox is
                # terminated; finalize them as CANCELLED so they stay
                # truthful after teardown.
                for record in self.run_ledger.list(session_id):
                    if not record.terminal:
                        self.run_ledger.cancel(session_id, record.n, message="agent closed")
            now = self.clock()
            rec.status = "closed"
            rec.ended_at = now
            rec.updated_at = now
            rec.current_turn_id = None
            rec.current_turn_n = None
            self.store.put(rec)
        if live is not None:
            live.proc.kill()
        if handle is not None and self.snapshot_hook is not None:
            try:
                self.snapshot_hook(rec, handle)
            except Exception:
                pass
        if handle is not None:
            try:
                self.backend.terminate(handle)
            except Exception:
                # The record is already terminal and keeps sandbox_id/root/
                # tags, so the reaper retries the terminate (SOR-80) while the
                # caller can still release capacity (slots, leases).
                pass
        stored = self.store.get(session_id)
        assert stored is not None
        return stored


def format_sse(event_id: int, payload: dict[str, Any]) -> str:
    return (
        f"id: {event_id}\n"
        f"event: {payload.get('type', 'message')}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n"
        "\n"
    )

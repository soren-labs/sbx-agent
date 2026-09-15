"""Session state machine and sandbox orchestration."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from control.backend import Process, SandboxBackend, SandboxSpec
from control.config import (
    DEFAULT_MODEL,
    IDLE_TIMEOUT_S,
    MAX_CONCURRENT,
    SANDBOX_USD_PER_S,
    TERMINAL_STATUSES,
    TURN_MAX_SECONDS,
)
from control.sandbox_io import drain, read_json, sandbox_env, write_file
from control.store import SessionRecord, SessionStore, empty_usage, merge_usage

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
    ) -> None:
        self.backend = backend
        self.store = store
        self.runner_cmd = list(runner_cmd)
        self.clock = clock or _utcnow
        self.max_concurrent = max_concurrent
        self.default_model = default_model
        self.idle_timeout_s = idle_timeout_s
        self.turn_max_seconds = turn_max_seconds
        self._lock = threading.RLock()
        self._live: dict[str, LiveTurn] = {}

    def runner(self, *args: str) -> list[str]:
        return [*self.runner_cmd, *args]

    def public(self, rec: SessionRecord) -> dict[str, Any]:
        now = self.clock()
        if rec.status in TERMINAL_STATUSES:
            end = rec.ended_at or rec.updated_at or now
        else:
            end = now
        sandbox_seconds = max(0.0, (end - rec.created_at).total_seconds())
        usage = dict(rec.usage)
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
        with self._lock:
            live = self.backend.list(tags={"owner": owner})
            if len(live) >= self.max_concurrent:
                raise ConcurrencyLimit()
            session_id = uuid.uuid4().hex
            tags = {"session_id": session_id, "owner": owner}
            # Preserve the exact P1 sandbox shape for legacy /api callers.
            # P2.1 provider sessions carry enough metadata for Modal to select
            # the correct image and reattach the per-account Secret on exec.
            if provider != "codex" or account_id != "auto":
                tags.update({"provider": provider, "account_id": account_id})
            secrets = [secret_name] if secret_name else []
            now = self.clock()
            rec = SessionRecord(
                id=session_id,
                title=title or "untitled",
                status="creating",
                created_at=now,
                updated_at=now,
                model=model or self.default_model,
                turns=0,
                usage=empty_usage(),
                messages=[],
                owner=owner,
                sandbox_tags=tags,
                last_activity_at=now,
            )
            # Publish the record before the sandbox exists: the reaper
            # distinguishes an in-flight create from an orphan sandbox via
            # this record (SOR-80), so ``backend.create`` cannot race it.
            self.store.put(rec)
            try:
                handle = self.backend.create(SandboxSpec(tags=tags, secrets=secrets))
            except Exception:
                self._mark_create_failed(rec)
                raise
            rec.sandbox_id = handle.id
            rec.sandbox_root = str(handle.root)
            rec.updated_at = self.clock()
            self.store.put(rec)

        try:
            init_args = ["init", "--auth", "auth_json", "--model", rec.model]
            init_env: dict[str, str] = {}
            if provider != "codex" or account_id != "auto":
                init_args += ["--provider", provider]
                if account_id != "auto":
                    init_args += ["--account-id", account_id]
                    init_env["SBX_ACCOUNT_ID"] = account_id
            init = self.backend.exec(
                handle,
                self.runner(*init_args),
                env=sandbox_env(handle, init_env),
            )
            code = drain(init)
            if code != 0:
                raise RuntimeError(f"runner init exited {code}")
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
            if stored is not None and stored.status in TERMINAL_STATUSES:
                # Closed concurrently while init ran — do not resurrect it.
                try:
                    self.backend.terminate(handle)
                except Exception:
                    pass
                raise SessionConflict("session_not_runnable")
            rec.status = "idle"
            now = self.clock()
            rec.updated_at = now
            rec.last_activity_at = now
            self.store.put(rec)
        return session_id

    def _mark_create_failed(self, rec: SessionRecord) -> None:
        """Terminal ``lost`` transition for a failed create; keeps sandbox_id."""
        with self._lock:
            stored = self.store.get(rec.id) or rec
            if stored.status in TERMINAL_STATUSES:
                return
            stored.status = "lost"
            stored.ended_at = self.clock()
            stored.updated_at = stored.ended_at
            self.store.put(stored)

    def post_message(self, session_id: str, text: str) -> str:
        with self._lock:
            rec = self.store.get(session_id)
            if rec is None:
                raise KeyError(session_id)
            if rec.status in TERMINAL_STATUSES:
                raise SessionConflict("session_not_runnable")
            if rec.status == "running" or rec.current_turn_id is not None:
                raise SessionConflict("turn_in_progress")
            if rec.status != "idle":
                # ``creating`` (or anything else non-idle) is not runnable.
                raise SessionConflict("session_not_runnable")
            handle = rec.handle()
            if handle is None:
                raise SessionConflict("session_not_runnable")
            n = rec.turns + 1
            turn_id = f"turn-{n}"
            now = self.clock()
            rec.status = "running"
            rec.current_turn_id = turn_id
            rec.current_turn_n = n
            rec.updated_at = now
            rec.messages.append({"role": "user", "text": text, "turn_id": turn_id, "ts": iso(now)})
            self.store.put(rec)

        rel = f"_prompt_{n}.md"
        try:
            write_file(self.backend, handle, rel, text)
            proc = self.backend.exec(
                handle,
                self.runner(
                    "turn",
                    "--n",
                    str(n),
                    "--message-file",
                    str(handle.root / rel),
                    "--max-seconds",
                    str(self.turn_max_seconds),
                ),
                env=sandbox_env(handle),
            )
        except Exception:
            self._rollback_turn(session_id, turn_id, handle)
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

    def _rollback_turn(self, session_id: str, turn_id: str, handle: Any) -> None:
        """Undo a queued turn whose write/exec never started (SOR-80).

        Deterministic end state: ``idle`` when the sandbox is still alive
        (runnable again) or ``lost`` when it is gone (terminal); the pending
        user message and turn markers are rolled back either way.
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
            rec.messages = [m for m in rec.messages if m.get("turn_id") != turn_id]
            rec.current_turn_id = None
            rec.current_turn_n = None
            rec.status = "idle" if alive else "lost"
            if not alive:
                rec.ended_at = now
            rec.updated_at = now
            rec.last_activity_at = now
            self.store.put(rec)

    def _watch_turn(self, session_id: str, turn_id: str, n: int, proc: Process) -> None:
        try:
            drain(proc)
        except Exception:
            pass
        self._finish_turn(session_id, turn_id, n)

    def _finish_turn(self, session_id: str, turn_id: str, n: int) -> None:
        with self._lock:
            rec = self.store.get(session_id)
            if rec is None or rec.status in TERMINAL_STATUSES:
                self._live.pop(session_id, None)
                return
            handle = rec.handle()
            payload = read_json(self.backend, handle, f"turns/{n}.json") if handle else None
            now = self.clock()
            if payload is not None:
                rec.usage = merge_usage(rec.usage, payload.get("usage"))
                rec.turns = max(rec.turns, int(payload.get("n") or n))
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
            if rec.current_turn_id == turn_id:
                rec.current_turn_id = None
                rec.current_turn_n = None
            if rec.status == "running":
                rec.status = "idle"
            rec.updated_at = now
            rec.last_activity_at = now
            self.store.put(rec)
            self._live.pop(session_id, None)

    def stop(self, session_id: str) -> str:
        with self._lock:
            rec = self.store.get(session_id)
            if rec is None:
                raise KeyError(session_id)
            handle = rec.handle()
            live = self._live.get(session_id)
            if rec.status == "running":
                rec.status = "idle"
                rec.current_turn_id = None
                rec.current_turn_n = None
                rec.updated_at = self.clock()
                rec.last_activity_at = rec.updated_at
                self.store.put(rec)
        if live is not None:
            live.proc.kill()
        if handle is not None:
            stop = self.backend.exec(handle, self.runner("stop"), env=sandbox_env(handle))
            drain(stop)
        with self._lock:
            self._live.pop(session_id, None)
            rec = self.store.get(session_id)
            return rec.status if rec else "closed"

    def close(self, session_id: str) -> SessionRecord:
        with self._lock:
            rec = self.store.get(session_id)
            if rec is None:
                raise KeyError(session_id)
            live = self._live.pop(session_id, None)
            handle = rec.handle()
            now = self.clock()
            rec.status = "closed"
            rec.ended_at = now
            rec.updated_at = now
            rec.current_turn_id = None
            rec.current_turn_n = None
            self.store.put(rec)
        if live is not None:
            live.proc.kill()
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

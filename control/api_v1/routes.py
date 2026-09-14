"""Public ``/v1`` endpoints (Cursor Cloud Agents shape, ``api-v1.yaml``).

``agent ≙ session``, ``run ≙ turn``: ``POST /v1/agents`` creates a session and
immediately queues its first run; follow-ups are new runs on the same agent.
All endpoints consume ``ports.*`` Protocols plus the shared SessionService
(``app.state.plane``); provider / account metadata is tracked in ``V1State``
until P2-C persists it on the session record.
"""

from __future__ import annotations

import asyncio
import json
import queue
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Header, Request
from fastapi.responses import Response

from control.api_v1 import router
from control.api_v1.deps import (
    admin_key,
    agents_key,
    api_key,
    get_key_store,
    get_plane,
    get_registry,
    get_scheduler,
    get_v1_state,
)
from control.api_v1.errors import V1ApiError, not_found
from control.api_v1.schemas import (
    VALID_SCOPES,
    CreateAccountRequest,
    CreateAgentRequest,
    CreateApiKeyRequest,
    CreateRunRequest,
    ProviderId,
    account_public,
    agent_public,
    api_key_public,
    usage_public,
)
from control.api_v1.state import AgentMeta, V1State
from control.devin_pool import ScheduleRefused
from control.ports import Account, AccountRegistry, ApiKey, ApiKeyStore, Scheduler
from control.sandbox_io import read_json, read_text, sandbox_env
from control.service import ConcurrencyLimit, SessionConflict, format_sse

AGENTS_PAGE_SIZE = 100
_TURN_ID_RE = re.compile(r"^turn-(\d+)$")
_RUN_ID_RE = re.compile(r"^run-(\d+)$")
_VERIFY_TAG = "account-verify"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _turn_n(turn_id: str | None) -> int | None:
    match = _TURN_ID_RE.match(turn_id or "")
    return int(match.group(1)) if match else None


def _run_n(run_id: str) -> int | None:
    match = _RUN_ID_RE.match(run_id or "")
    return int(match.group(1)) if match else None


def _known_run_ns(rec: Any) -> set[int]:
    ns = set(range(1, int(rec.turns) + 1))
    for message in rec.messages:
        n = _turn_n(message.get("turn_id"))
        if n is not None:
            ns.add(n)
    if rec.current_turn_n is not None:
        ns.add(int(rec.current_turn_n))
    return ns


def _turn_payload(plane: Any, rec: Any, n: int) -> dict[str, Any] | None:
    """Read ``turns/<n>.json`` when the sandbox is still reachable."""
    backend = getattr(plane, "backend", None)
    handle = rec.handle()
    if backend is None or handle is None:
        return None
    try:
        poll = backend.poll(handle)
    except Exception:
        return None
    if not poll.alive:
        return None
    try:
        payload = read_json(backend, handle, f"turns/{n}.json")
    except Exception:
        return None
    return payload


def _run_public(
    plane: Any, pub: dict[str, Any], rec: Any, n: int, cancelled: set[int]
) -> dict[str, Any]:
    """Derive a Cursor-shaped Run from session record + ``turns/<n>.json``."""
    turn_id = f"turn-{n}"
    created_at: str = pub["created_at"]
    updated_at: str = pub["updated_at"]
    result_text: str | None = None
    for message in rec.messages:
        if message.get("turn_id") != turn_id:
            continue
        if message.get("role") == "user":
            created_at = str(message.get("ts") or created_at)
        elif message.get("role") == "assistant":
            result_text = str(message.get("text") or "") or None
            updated_at = str(message.get("ts") or updated_at)

    usage: dict[str, Any] | None = None
    if rec.current_turn_n == n and rec.status not in ("closed", "timed_out", "lost"):
        status = "RUNNING"
    elif n in cancelled:
        status = "CANCELLED"
    elif n <= int(rec.turns):
        payload = _turn_payload(plane, rec, n)
        if payload is not None:
            turn_status = str(payload.get("status") or "")
            if turn_status == "success":
                status = "FINISHED"
            elif turn_status == "timeout":
                status = "EXPIRED"
            else:
                status = "ERROR"
            if payload.get("message"):
                result_text = str(payload["message"])
            if isinstance(payload.get("usage"), dict):
                usage = payload["usage"]
        else:
            status = "FINISHED"
    elif rec.status in ("timed_out", "lost"):
        status = "EXPIRED"
    else:
        status = "CANCELLED"

    run: dict[str, Any] = {
        "id": f"run-{n}",
        "agent_id": rec.id,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "result": {"text": result_text} if result_text else None,
    }
    if usage is not None:
        run["usage"] = usage_public(usage)
    return run


def _runs(plane: Any, rec: Any, v1: V1State) -> list[dict[str, Any]]:
    pub = plane.public(rec)
    cancelled = v1.cancelled(rec.id)
    return [_run_public(plane, pub, rec, n, cancelled) for n in sorted(_known_run_ns(rec))]


def _require_agent(plane: Any, agent_id: str) -> Any:
    rec = plane.get(agent_id)
    if rec is None:
        raise not_found("agent not found")
    return rec


def _require_run(plane: Any, rec: Any, run_id: str, v1: V1State) -> dict[str, Any]:
    n = _run_n(run_id)
    if n is None or n not in _known_run_ns(rec):
        raise not_found("run not found")
    pub = plane.public(rec)
    return _run_public(plane, pub, rec, n, v1.cancelled(rec.id))


# ---------------------------------------------------------------- agents


def _raise_schedule_error(
    error: str | None,
    *,
    retry_after: float | None,
    provider: str,
    requested: str,
) -> None:
    if not error:
        return
    if error == "invalid_provider":
        raise V1ApiError(400, "invalid_provider", f"unknown provider {provider!r}")
    if error in ("account_busy", "account_unavailable"):
        raise V1ApiError(409, error, f"account {requested!r} cannot take the run")
    raise V1ApiError(
        429,
        "provider_exhausted",
        f"no account available for provider {provider!r}",
        retry_after=retry_after,
    )


def _release_agent_lease(v1: V1State, agent_id: str) -> None:
    lease = v1.pop_lease(agent_id)
    if lease is not None:
        lease.release()


@router.post("/agents", status_code=201)
def create_agent(
    body: CreateAgentRequest,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
    scheduler: Scheduler = Depends(get_scheduler),
    v1: V1State = Depends(get_v1_state),
) -> dict[str, Any]:
    provider = body.agent.provider
    requested = body.agent.account_id or "auto"

    # P2.1's Devin pool exposes an atomic acquire() in addition to the frozen
    # consultative Scheduler.decide() port.  Use it when available so two
    # concurrent POSTs cannot both observe the same free slot.
    lease = None
    acquire = getattr(scheduler, "acquire", None)
    if callable(acquire):
        try:
            lease = acquire(provider=provider, account=requested)
        except ScheduleRefused as exc:
            _raise_schedule_error(
                exc.error,
                retry_after=exc.retry_after,
                provider=provider,
                requested=requested,
            )
            raise AssertionError("unreachable")
        account = lease.account
    else:
        decision = scheduler.decide(provider=provider, account=requested)
        _raise_schedule_error(
            decision.error,
            retry_after=decision.retry_after,
            provider=provider,
            requested=requested,
        )
        account = decision.account

    resolved = account.id if account is not None else requested
    secret_name = None
    if account is not None:
        secret_name = account.secret_name or None

    try:
        session_id = plane.create_session(
            owner=key.id,
            title=body.name,
            model=body.agent.model,
            provider=provider,
            account_id=resolved,
            secret_name=secret_name,
        )
    except ConcurrencyLimit as exc:
        if lease is not None:
            lease.release()
        raise V1ApiError(
            429, "concurrency_limit", "per-key concurrent sandbox cap reached"
        ) from exc
    except Exception:
        if lease is not None:
            lease.release()
        raise

    if lease is not None:
        v1.set_lease(session_id, lease)
    v1.set_meta(
        session_id,
        AgentMeta(
            provider=provider,
            account_id=resolved,
            name=body.name,
            idle_timeout_s=body.idle_timeout_s,
        ),
    )
    if account is not None:
        try:
            registry.touch(account.id, _iso_now())
        except KeyError:
            pass
    try:
        turn_id = plane.post_message(session_id, body.prompt.text)
    except SessionConflict as exc:
        try:
            plane.close(session_id)
        finally:
            _release_agent_lease(v1, session_id)
        raise V1ApiError(exc.code, exc.error, exc.error) from exc
    except KeyError as exc:
        try:
            plane.close(session_id)
        finally:
            _release_agent_lease(v1, session_id)
        raise not_found("agent not found after create") from exc
    n = _turn_n(turn_id) or 1
    rec = _require_agent(plane, session_id)
    agent = agent_public(plane.public(rec), v1.get_meta(session_id))
    run = _run_public(plane, plane.public(rec), rec, n, v1.cancelled(session_id))
    return {"agent": agent, "run": run}


@router.get("/agents")
def list_agents(
    provider: ProviderId | None = None,
    account_id: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
) -> dict[str, Any]:
    start = 0
    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError:
            raise V1ApiError(400, "invalid_provider", "malformed cursor") from None
    agents = [agent_public(pub, v1.get_meta(pub["id"])) for pub in plane.list_sessions()]
    agents = [
        a
        for a in agents
        if (provider is None or a["provider"] == provider)
        and (account_id is None or a["account_id"] == account_id)
        and (status is None or a["status"] == status)
    ]
    agents.sort(key=lambda a: (a["created_at"], a["id"]))
    page = agents[start : start + AGENTS_PAGE_SIZE]
    next_cursor = str(start + AGENTS_PAGE_SIZE) if start + AGENTS_PAGE_SIZE < len(agents) else None
    return {"agents": page, "next_cursor": next_cursor}


@router.get("/agents/{agent_id}")
def get_agent(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    return agent_public(plane.public(rec), v1.get_meta(agent_id))


@router.delete("/agents/{agent_id}")
def delete_agent(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
) -> dict[str, Any]:
    try:
        rec = plane.close(agent_id)
    except KeyError:
        raise not_found("agent not found") from None
    _release_agent_lease(v1, agent_id)
    return agent_public(plane.public(rec), v1.get_meta(agent_id))


# ------------------------------------------------------------------- runs


@router.post("/agents/{agent_id}/runs", status_code=201)
def create_run(
    agent_id: str,
    body: CreateRunRequest,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
) -> dict[str, Any]:
    _require_agent(plane, agent_id)
    try:
        turn_id = plane.post_message(agent_id, body.prompt.text)
    except KeyError:
        raise not_found("agent not found") from None
    except SessionConflict as exc:
        raise V1ApiError(exc.code, exc.error, exc.error) from exc
    n = _turn_n(turn_id) or 0
    rec = _require_agent(plane, agent_id)
    pub = plane.public(rec)
    return _run_public(plane, pub, rec, n, v1.cancelled(agent_id))


@router.get("/agents/{agent_id}/runs")
def list_runs(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    return {"runs": _runs(plane, rec, v1)}


@router.get("/agents/{agent_id}/runs/{run_id}")
def get_run(
    agent_id: str,
    run_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    return _require_run(plane, rec, run_id, v1)


@router.post("/agents/{agent_id}/runs/{run_id}/cancel")
def cancel_run(
    agent_id: str,
    run_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    n = _run_n(run_id)
    if n is None or n not in _known_run_ns(rec):
        raise not_found("run not found")
    if rec.current_turn_n == n and rec.status == "running":
        try:
            plane.stop(agent_id)
        except KeyError:
            raise not_found("agent not found") from None
        v1.mark_cancelled(agent_id, n)
        rec = _require_agent(plane, agent_id)
    return _require_run(plane, rec, run_id, v1)


# ------------------------------------------------------------------- SSE


def _belongs_to_run(obj: dict[str, Any], run_n: int, current_turn: int) -> tuple[bool, int]:
    """Track turn boundaries via ``sbx.turn_started``; report membership.

    Lines preceding the first ``sbx.turn_started`` (e.g. ``sbx.session_meta``)
    are attributed to run 1. ``id`` keeps the absolute events.jsonl line number
    so ``Last-Event-ID`` resume stays consistent with ``/api/*`` semantics.
    """
    if obj.get("type") == "sbx.turn_started":
        try:
            current_turn = int(obj.get("n") or 0)
        except (TypeError, ValueError):
            pass
    return (current_turn == run_n or (run_n == 1 and current_turn == 0)), current_turn


def _parse_event_line(raw: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
        return {"type": "error", "message": raw}
    except json.JSONDecodeError:
        return {"type": "error", "message": "bad json in event stream"}


@router.get("/agents/{agent_id}/runs/{run_id}/stream")
async def stream_run(
    request: Request,
    agent_id: str,
    run_id: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
    v1: V1State = Depends(get_v1_state),
) -> Any:
    from control.app import DisconnectAwareStreamingResponse

    rec = _require_agent(plane, agent_id)
    n = _run_n(run_id)
    if n is None or n not in _known_run_ns(rec):
        raise not_found("run not found")
    try:
        last_id = int(last_event_id) if last_event_id else 0
    except ValueError:
        last_id = 0
    start_line = max(1, last_id + 1)

    backend = getattr(plane, "backend", None)
    handle = rec.handle()
    poll = None
    if backend is not None and handle is not None:
        try:
            poll = backend.poll(handle)
        except Exception:
            poll = None
    keepalive_s: float = getattr(request.app.state, "keepalive_s", 15.0)

    async def gen() -> AsyncIterator[str]:
        proc: Any = None
        current_turn = 0
        try:
            yield ": keepalive\n\n"
            if backend is not None and handle is not None and poll is not None and poll.alive:
                proc = await asyncio.to_thread(
                    backend.exec,
                    handle,
                    ["tail", "-n", "+1", "-F", str(handle.root / "events.jsonl")],
                    sandbox_env(handle),
                )
                line_q: queue.Queue[tuple[str, str | None]] = queue.Queue()

                def _reader() -> None:
                    try:
                        for line in proc.stdout:
                            line_q.put(("line", line))
                    except Exception:
                        pass
                    finally:
                        line_q.put(("eof", None))

                threading.Thread(target=_reader, daemon=True, name="sbx-v1-sse-tail").start()

                lineno = 0
                next_ka = time.monotonic() + keepalive_s
                while True:
                    try:
                        kind, payload = line_q.get_nowait()
                    except queue.Empty:
                        now = time.monotonic()
                        if now >= next_ka:
                            yield ": keepalive\n\n"
                            next_ka = now + keepalive_s
                        await asyncio.sleep(0.05)
                        continue
                    if kind == "eof":
                        break
                    raw = payload or ""
                    if not raw.strip():
                        continue
                    lineno += 1
                    obj = _parse_event_line(raw)
                    emit, current_turn = _belongs_to_run(obj, n, current_turn)
                    if emit and lineno >= start_line:
                        yield format_sse(lineno, obj)
                    now = time.monotonic()
                    if now >= next_ka:
                        yield ": keepalive\n\n"
                        next_ka = now + keepalive_s
                return

            # Sandbox unreachable: replay the run's slice if the file is
            # locally readable, then keep the stream open like /api/* does.
            lines: list[str] = []
            if backend is not None and handle is not None:
                try:
                    text = read_text(backend, handle, "events.jsonl")
                except Exception:
                    text = None
                if text:
                    lines = text.splitlines()
            lineno = 0
            for raw in lines:
                if not raw.strip():
                    continue
                lineno += 1
                obj = _parse_event_line(raw)
                emit, current_turn = _belongs_to_run(obj, n, current_turn)
                if emit and lineno >= start_line:
                    yield format_sse(lineno, obj)
            while True:
                await asyncio.sleep(keepalive_s)
                yield ": keepalive\n\n"
        finally:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass

    return DisconnectAwareStreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------ misc


@router.get("/agents/{agent_id}/usage")
def get_agent_usage(
    agent_id: str,
    key: ApiKey = Depends(agents_key),
    plane: Any = Depends(get_plane),
) -> dict[str, Any]:
    rec = _require_agent(plane, agent_id)
    pub = plane.public(rec)
    return {
        "usage": usage_public(pub.get("usage")),
        "cost_estimate_usd": pub.get("cost_estimate_usd", 0.0),
        "sandbox_seconds": pub.get("sandbox_seconds", 0.0),
    }


@router.get("/models")
def list_models(
    key: ApiKey = Depends(agents_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Advertised models come from account declarations; availability counts
    active accounts with a free slot that list the model."""
    counts: dict[tuple[str, str], int] = {}
    for account in registry.list():
        free = (
            account.status == "active"
            and registry.running_count(account.id) < account.max_concurrent
        )
        for model in account.models:
            key_ = (account.provider, model)
            counts[key_] = counts.get(key_, 0) + (1 if free else 0)
    models = [
        {"provider": provider, "model": model, "accounts_available": count}
        for (provider, model), count in sorted(counts.items())
    ]
    return {"models": models}


@router.get("/me")
def get_me(key: ApiKey = Depends(api_key)) -> dict[str, Any]:
    return {"key_id": key.id, "label": key.label, "scopes": list(key.scopes)}


# --------------------------------------------------------------- accounts


@router.get("/accounts")
def list_accounts(
    provider: ProviderId | None = None,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    return {
        "accounts": [
            account_public(account, registry.running_count(account.id))
            for account in registry.list(provider)
        ]
    }


@router.post("/accounts", status_code=201)
def create_account(
    body: CreateAccountRequest,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    account = Account(
        id=f"acct-{body.provider}-{uuid.uuid4().hex[:8]}",
        provider=body.provider,
        label=body.label,
        max_concurrent=body.max_concurrent,
        models=tuple(body.models),
        created_at=_iso_now(),
    )
    registry.put(account)
    if body.credential is not None:
        files = body.credential.get("files")
        if files is not None and (
            not isinstance(files, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in files.items())
        ):
            raise V1ApiError(400, "invalid_provider", "credential.files must be a string map")
        registry.put_credential_blob(
            account.id,
            {"provider": body.provider, "files": dict(files or {})},
        )
    return account_public(account, registry.running_count(account.id))


@router.get("/accounts/{account_id}")
def get_account(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    account = registry.get(account_id)
    if account is None:
        raise not_found("account not found")
    return account_public(account, registry.running_count(account.id))


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    registry: AccountRegistry = Depends(get_registry),
) -> Response:
    if registry.get(account_id) is None:
        raise not_found("account not found")
    registry.remove(account_id)
    return Response(status_code=204)


@router.post("/accounts/{account_id}/verify")
def verify_account(
    account_id: str,
    key: ApiKey = Depends(admin_key),
    plane: Any = Depends(get_plane),
    registry: AccountRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Probe the stored credential in a throwaway sandbox.

    Runs ``runner init`` with ``SBX_ACCOUNT_CREDENTIAL`` / ``SBX_ACCOUNT_ID``
    injected: the blob is restored under ``$SBX_WORK/home`` and a non-zero init
    marks the account ``invalid``. When the plane exposes no usable backend the
    account is simply marked ``active`` (real per-provider CLI probes land with
    the P2-B adapters).
    """
    account = registry.get(account_id)
    if account is None:
        raise not_found("account not found")
    blob = registry.get_credential_blob(account_id)
    backend = getattr(plane, "backend", None)
    runner = getattr(plane, "runner", None)
    if backend is None or runner is None:
        return account_public(
            registry.mark_status(account_id, "active", last_error=None),
            registry.running_count(account_id),
        )
    handle = None
    try:
        from control.backend import SandboxSpec

        handle = backend.create(
            SandboxSpec(tags={"purpose": _VERIFY_TAG, "account_id": account_id})
        )
        env = sandbox_env(
            handle,
            {
                "SBX_ACCOUNT_ID": account_id,
                "SBX_ACCOUNT_CREDENTIAL": json.dumps(blob or {}),
            },
        )
        model = getattr(plane, "default_model", None) or "gpt-5.6-luna"
        argv = runner("init", "--auth", "auth_json", "--model", model)
        proc = backend.exec(handle, argv, env=env)
        for _ in proc.stdout:
            pass
        code = proc.wait()
    except Exception:
        code = -1
    finally:
        if handle is not None:
            try:
                backend.terminate(handle)
            except Exception:
                pass
    if code == 0:
        updated = registry.mark_status(account_id, "active", last_error=None)
    elif code == 5:
        updated = registry.mark_status(account_id, "invalid", last_error="auth_invalid")
    elif code > 0:
        updated = registry.mark_status(account_id, "invalid", last_error="init_failed")
    else:
        updated = account
    return account_public(updated, registry.running_count(account_id))


# -------------------------------------------------------------- api keys


@router.get("/api-keys")
def list_api_keys(
    key: ApiKey = Depends(admin_key),
    store: ApiKeyStore = Depends(get_key_store),
) -> dict[str, Any]:
    return {"api_keys": [api_key_public(k) for k in store.list()]}


@router.post("/api-keys", status_code=201)
def create_api_key(
    body: CreateApiKeyRequest | None = None,
    key: ApiKey = Depends(admin_key),
    store: ApiKeyStore = Depends(get_key_store),
) -> dict[str, Any]:
    body = body or CreateApiKeyRequest()
    scopes = body.scopes if body.scopes is not None else ["agents"]
    if any(scope not in VALID_SCOPES for scope in scopes):
        raise V1ApiError(400, "invalid_provider", f"unknown scope; allowed: {list(VALID_SCOPES)}")
    record, token = store.create(label=body.label, scopes=scopes)
    return {**api_key_public(record), "key": token}


@router.delete("/api-keys/{key_id}", status_code=204)
def delete_api_key(
    key_id: str,
    key: ApiKey = Depends(admin_key),
    store: ApiKeyStore = Depends(get_key_store),
) -> Response:
    if not store.revoke(key_id):
        raise not_found("api key not found")
    return Response(status_code=204)

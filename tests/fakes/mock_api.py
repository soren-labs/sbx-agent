"""In-memory FastAPI mock of docs/contracts/api.yaml.

Start: ``python -m tests.fakes.mock_api --port 8787``

HTTP Basic user/password default to ``sbx`` / ``sbx`` (local mock only, not a secret).
Override with ``SBX_API_USER`` / ``SBX_API_PASSWORD``.

Serves ``web/`` as static files at ``/`` so the chat page can develop against this mock.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from control.ports import Account
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from tests.fakes.fake_ports import (
    InMemoryAccountRegistry,
    InMemoryApiKeyStore,
    InMemoryScheduler,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "events"
FIXTURE = FIXTURES / "success.jsonl"
RESUME_FIXTURE = FIXTURES / "resume.jsonl"
WEB_DIR = Path(__file__).resolve().parents[2] / "web"
MAX_CONCURRENT = 2
SSE_INTERVAL_S = float(os.environ.get("SBX_SSE_INTERVAL_SECONDS", "0.2"))
SSE_KEEPALIVE_S = float(os.environ.get("SBX_SSE_KEEPALIVE_SECONDS", "15"))
SSE_RETRY_MS = int(os.environ.get("SBX_SSE_RETRY_MS", "250"))
SSE_DROP_FIRST_AFTER = int(os.environ.get("SBX_SSE_DROP_FIRST_AFTER", "0"))
CREATE_DELAY_S = float(os.environ.get("SBX_MOCK_CREATE_DELAY_S", "0"))
TURN_EVENT_INTERVAL_S = float(os.environ.get("SBX_MOCK_TURN_INTERVAL_SECONDS", "0.25"))
BASIC_USER = os.environ.get("SBX_API_USER", "sbx")
BASIC_PASSWORD = os.environ.get("SBX_API_PASSWORD", "sbx")
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_PROVIDER = "codex"

# Canned per-provider model lists for /api/providers (contract: api.yaml).
PROVIDER_MODELS: dict[str, list[str]] = {
    "codex": ["gpt-5.6-luna", "gpt-5.3-codex"],
    "antigravity": ["gemini-3-pro", "claude-sonnet-4.5"],
    "grok": ["grok-build", "grok-4.1"],
    "opencode": ["openai/gpt-5.6-luna", "opencode/claude-sonnet-4-5"],
    "devin": ["swe-2-high", "swe-2"],
}

app = FastAPI(title="sbx-browser mock API", version="0.1.0")
security = HTTPBasic(auto_error=False)
_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}
_sse_conn_counts: dict[str, int] = {}
_accounts = InMemoryAccountRegistry()
_scheduler = InMemoryScheduler(_accounts)
_api_keys = InMemoryApiKeyStore()

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Authorization", "Last-Event-ID", "Content-Type"],
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _usage() -> dict[str, int]:
    return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}


def _merge_usage(base: dict[str, int], extra: dict[str, Any] | None) -> dict[str, int]:
    if not extra:
        return dict(base)
    out = dict(base)
    for key in (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
    ):
        if key in extra:
            out[key] = int(extra[key])
    return out


def _cost(usage: dict[str, int]) -> float:
    return round(
        usage.get("input_tokens", 0) * 1.25e-6
        + usage.get("cached_input_tokens", 0) * 0.125e-6
        + usage.get("output_tokens", 0) * 1.0e-5,
        6,
    )


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.is_file():
        return events
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _load_fixture_events() -> list[dict[str, Any]]:
    return _load_jsonl(FIXTURE)


def _usage_from_events(events: list[dict[str, Any]]) -> dict[str, int]:
    usage = _usage()
    for ev in events:
        if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            usage = _merge_usage(usage, ev["usage"])
        if ev.get("type") == "sbx.turn_finished" and isinstance(ev.get("usage"), dict):
            usage = _merge_usage(usage, ev["usage"])
    return usage


def _enrich_turn(events: list[dict[str, Any]], n: int, reasoning_text: str) -> list[dict[str, Any]]:
    """Wrap Codex JSONL with runner sbx.* events and a reasoning item (UI coverage)."""
    out: list[dict[str, Any]] = [{"type": "sbx.turn_started", "n": n}]
    inserted_reason = False
    last_usage: dict[str, Any] | None = None
    for ev in events:
        out.append(ev)
        if not inserted_reason and ev.get("type") == "turn.started":
            out.append(
                {
                    "type": "item.completed",
                    "item": {
                        "id": f"item_reasoning_{n}",
                        "type": "reasoning",
                        "text": reasoning_text,
                    },
                }
            )
            inserted_reason = True
        if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            last_usage = ev["usage"]
    out.append(
        {
            "type": "sbx.turn_finished",
            "status": "success",
            "exit_code": 0,
            "duration_s": 1.4,
            "usage": last_usage or _usage(),
        }
    )
    return out


def _public(sess: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    closed = sess["status"] in {"closed", "timed_out", "lost"}
    end = sess["updated_at"] if closed else now
    sandbox_seconds = max(0.0, (end - sess["created_at"]).total_seconds())
    usage = dict(sess["usage"])
    return {
        "id": sess["id"],
        "title": sess["title"],
        "status": sess["status"],
        "created_at": _iso(sess["created_at"]),
        "updated_at": _iso(sess["updated_at"]),
        "provider": sess["provider"],
        "account_id": sess["account_id"],
        "model": sess["model"],
        "turns": sess["turns"],
        "usage": usage,
        "cost_estimate_usd": _cost(usage),
        "sandbox_seconds": sandbox_seconds,
        "messages": list(sess["messages"]),
    }


def require_basic(credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "code": 401},
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = secrets.compare_digest(credentials.username, BASIC_USER)
    pass_ok = secrets.compare_digest(credentials.password, BASIC_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "code": 401},
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


class CreateSessionRequest(BaseModel):
    title: str | None = None
    provider: str | None = None
    account_id: str | None = None
    model: str | None = None


class PostMessageRequest(BaseModel):
    text: str = Field(min_length=1)


class CreateAccountRequest(BaseModel):
    provider: str
    label: str
    credential: dict[str, Any] | None = None
    max_concurrent: int = 1
    models: list[str] = Field(default_factory=list)


class CreateApiKeyRequest(BaseModel):
    label: str = ""
    scopes: list[str] = Field(default_factory=lambda: ["agents"])


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail,
            headers=exc.headers,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": str(exc.detail), "code": exc.status_code},
        headers=exc.headers,
    )


def _new_session(
    *,
    title: str,
    model: str,
    provider: str = DEFAULT_PROVIDER,
    account_id: str | None = None,
    status: str = "idle",
    created_at: datetime | None = None,
    events: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    turns: int = 0,
    usage: dict[str, int] | None = None,
    sid: str | None = None,
) -> dict[str, Any]:
    now = created_at or _now()
    evs = events if events is not None else _load_fixture_events()
    return {
        "id": sid or uuid.uuid4().hex,
        "title": title,
        "status": status,
        "created_at": now,
        "updated_at": now,
        "provider": provider,
        "account_id": account_id or f"acct-{provider}-1",
        "model": model,
        "turns": turns,
        "usage": usage if usage is not None else _usage_from_events(evs),
        "messages": list(messages or []),
        "events": list(evs),
        "current_turn_id": None,
        "timer": None,
        "stop_requested": False,
    }


def _promote_creating(sid: str, delay: float) -> None:
    if delay <= 0:
        return
    time.sleep(delay)
    with _lock:
        sess = _sessions.get(sid)
        if sess is None:
            return
        if sess["status"] == "creating":
            sess["status"] = "idle"
            sess["updated_at"] = _now()


@app.post("/api/sessions", status_code=201)
def create_session(
    body: CreateSessionRequest | None = None,
    _: str = Depends(require_basic),
) -> dict[str, str]:
    body = body or CreateSessionRequest()
    with _lock:
        active = sum(
            1 for s in _sessions.values() if s["status"] in {"creating", "idle", "running"}
        )
        if active >= MAX_CONCURRENT:
            raise HTTPException(
                status_code=429,
                detail={"error": "concurrency_limit", "code": 429},
            )
        provider = body.provider or DEFAULT_PROVIDER
        if provider not in PROVIDER_MODELS:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_provider", "code": 400},
            )
        decision = _scheduler.decide(provider=provider, account=body.account_id or "auto")
        if decision.error == "account_busy":
            raise HTTPException(
                status_code=409,
                detail={"error": "account_busy", "code": 409},
            )
        if decision.error == "account_unavailable":
            raise HTTPException(
                status_code=409,
                detail={"error": "account_unavailable", "code": 409},
            )
        if decision.error == "provider_exhausted":
            raise HTTPException(
                status_code=429,
                detail={"error": "provider_exhausted", "code": 429},
            )
        account = decision.account
        account_id = account.id if account is not None else f"acct-{provider}-1"
        if account is not None:
            _accounts.touch(account.id, _iso(_now()))
        initial = "creating" if CREATE_DELAY_S > 0 else "idle"
        sess = _new_session(
            title=body.title or "untitled",
            model=body.model or PROVIDER_MODELS[provider][0],
            provider=provider,
            account_id=account_id,
            status=initial,
            events=_enrich_turn(
                _load_fixture_events(),
                1,
                "I will write a small file in /work and verify it.",
            ),
            usage=_usage(),
        )
        _sessions[sess["id"]] = sess
        sid = sess["id"]
    if CREATE_DELAY_S > 0:
        threading.Thread(target=_promote_creating, args=(sid, CREATE_DELAY_S), daemon=True).start()
    return {"session_id": sid}


@app.get("/api/sessions")
def list_sessions(_: str = Depends(require_basic)) -> list[dict[str, Any]]:
    with _lock:
        return [_public(s) for s in _sessions.values()]


def _get(sid: str) -> dict[str, Any]:
    sess = _sessions.get(sid)
    if sess is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "code": 404})
    return sess


@app.get("/api/sessions/{sid}")
def get_session(sid: str, _: str = Depends(require_basic)) -> dict[str, Any]:
    with _lock:
        return _public(_get(sid))


def _assistant_text(events: list[dict[str, Any]]) -> str:
    text = "mock turn complete"
    for ev in events:
        item = ev.get("item") if isinstance(ev.get("item"), dict) else None
        if ev.get("type") == "item.completed" and item and item.get("type") == "agent_message":
            text = str(item.get("text") or text)
    return text


def _run_turn(sid: str, turn_id: str, events: list[dict[str, Any]]) -> None:
    """Append turn events over time so an already-open SSE stream can follow."""
    for ev in events:
        time.sleep(TURN_EVENT_INTERVAL_S)
        with _lock:
            sess = _sessions.get(sid)
            if sess is None:
                return
            if sess.get("current_turn_id") != turn_id or sess.get("stop_requested"):
                return
            sess["events"].append(ev)
            sess["updated_at"] = _now()
            if ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
                sess["usage"] = _merge_usage(sess["usage"], ev["usage"])
            if ev.get("type") == "sbx.turn_finished" and isinstance(ev.get("usage"), dict):
                sess["usage"] = _merge_usage(sess["usage"], ev["usage"])
    with _lock:
        sess = _sessions.get(sid)
        if sess is None:
            return
        if sess.get("current_turn_id") != turn_id:
            return
        sess["status"] = "idle"
        sess["current_turn_id"] = None
        sess["stop_requested"] = False
        sess["turns"] += 1
        sess["updated_at"] = _now()
        sess["messages"].append(
            {
                "role": "assistant",
                "text": _assistant_text(events),
                "turn_id": turn_id,
                "ts": _iso(_now()),
            }
        )


@app.post("/api/sessions/{sid}/messages", status_code=202)
def post_message(
    sid: str,
    body: PostMessageRequest,
    _: str = Depends(require_basic),
) -> dict[str, str]:
    with _lock:
        sess = _get(sid)
        if sess["status"] in {"closed", "timed_out", "lost"}:
            raise HTTPException(
                status_code=409,
                detail={"error": "session_not_runnable", "code": 409},
            )
        if sess["status"] == "running":
            raise HTTPException(
                status_code=409,
                detail={"error": "turn_in_progress", "code": 409},
            )
        turn_n = sess["turns"] + 1
        turn_id = f"turn-{turn_n}"
        sess["status"] = "running"
        sess["current_turn_id"] = turn_id
        sess["stop_requested"] = False
        sess["updated_at"] = _now()
        sess["messages"].append(
            {
                "role": "user",
                "text": body.text,
                "turn_id": turn_id,
                "ts": _iso(_now()),
            }
        )
        source = _load_jsonl(RESUME_FIXTURE if turn_n > 1 else FIXTURE)
        reason = (
            "The user asked to continue; I will update the existing workspace file."
            if turn_n > 1
            else "I will write a small file in /work and verify it."
        )
        queued = _enrich_turn(source, turn_n, reason)
        timer = threading.Thread(target=_run_turn, args=(sid, turn_id, queued), daemon=True)
        sess["timer"] = timer
        timer.start()
    return {"turn_id": turn_id}


@app.post("/api/sessions/{sid}/stop", status_code=202)
def stop_session(sid: str, _: str = Depends(require_basic)) -> dict[str, str]:
    with _lock:
        sess = _get(sid)
        if sess["status"] == "running":
            sess["status"] = "idle"
            sess["current_turn_id"] = None
            sess["stop_requested"] = True
            sess["updated_at"] = _now()
            sess["events"].append(
                {
                    "type": "sbx.turn_finished",
                    "status": "timeout",
                    "exit_code": 3,
                    "duration_s": 0.2,
                    "usage": dict(sess["usage"]),
                }
            )
        return {"status": sess["status"]}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str, _: str = Depends(require_basic)) -> dict[str, Any]:
    with _lock:
        sess = _get(sid)
        sess["status"] = "closed"
        sess["current_turn_id"] = None
        sess["stop_requested"] = True
        sess["updated_at"] = _now()
        return _public(sess)


def _account_public(account: Any) -> dict[str, Any]:
    """Serialize a ports.Account to the api.yaml Account shape (no credentials)."""
    return {
        "id": account.id,
        "provider": account.provider,
        "label": account.label,
        "status": account.status,
        "max_concurrent": account.max_concurrent,
        "running": _accounts.running_count(account.id),
        "models": list(account.models),
        "created_at": account.created_at,
        "last_used_at": account.last_used_at,
        "cooldown_until": account.cooldown_until,
        "last_error": account.last_error,
    }


def _api_key_public(key: Any) -> dict[str, Any]:
    return {
        "id": key.id,
        "label": key.label,
        "scopes": list(key.scopes),
        "created_at": key.created_at,
        "revoked_at": key.revoked_at,
    }


@app.get("/api/providers")
def list_providers(_: str = Depends(require_basic)) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for provider, models in PROVIDER_MODELS.items():
        accounts = _accounts.list(provider)
        available = sum(
            1
            for a in accounts
            if a.status == "active" and _accounts.running_count(a.id) < a.max_concurrent
        )
        out.append(
            {
                "provider": provider,
                "models": models,
                "accounts_total": len(accounts),
                "accounts_available": available,
            }
        )
    return out


@app.get("/api/accounts")
def list_accounts(
    provider: str | None = None,
    _: str = Depends(require_basic),
) -> list[dict[str, Any]]:
    return [_account_public(a) for a in _accounts.list(provider)]


@app.post("/api/accounts", status_code=201)
def create_account(
    body: CreateAccountRequest,
    _: str = Depends(require_basic),
) -> dict[str, Any]:
    if body.provider not in PROVIDER_MODELS:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_provider", "code": 400},
        )
    account = Account(
        id=f"acct-{body.provider}-{uuid.uuid4().hex[:8]}",
        provider=body.provider,
        label=body.label,
        max_concurrent=body.max_concurrent,
        models=tuple(body.models),
        created_at=_iso(_now()),
    )
    _accounts.put(account)
    if body.credential is not None:
        # Stored for the mock scheduler only; never returned or logged.
        _accounts.put_credential_blob(
            account.id,
            {"provider": body.provider, "files": dict(body.credential.get("files") or {})},
        )
    return _account_public(_accounts.get(account.id))


@app.get("/api/accounts/{account_id}")
def get_account(account_id: str, _: str = Depends(require_basic)) -> dict[str, Any]:
    account = _accounts.get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "code": 404})
    return _account_public(account)


@app.delete("/api/accounts/{account_id}", status_code=204)
def delete_account(account_id: str, _: str = Depends(require_basic)) -> None:
    if _accounts.get(account_id) is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "code": 404})
    _accounts.remove(account_id)


@app.post("/api/accounts/{account_id}/verify")
def verify_account(account_id: str, _: str = Depends(require_basic)) -> dict[str, Any]:
    account = _accounts.get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "code": 404})
    # Mock verify: no real credential probe; mark the account active.
    verified = _accounts.mark_status(account_id, "active", last_error=None)
    return _account_public(verified)


@app.get("/api/api-keys")
def list_api_keys(_: str = Depends(require_basic)) -> list[dict[str, Any]]:
    return [_api_key_public(k) for k in _api_keys.list()]


@app.post("/api/api-keys", status_code=201)
def create_api_key(
    body: CreateApiKeyRequest | None = None,
    _: str = Depends(require_basic),
) -> dict[str, Any]:
    body = body or CreateApiKeyRequest()
    record, token = _api_keys.create(label=body.label, scopes=body.scopes)
    return {**_api_key_public(record), "key": token}


@app.delete("/api/api-keys/{key_id}", status_code=204)
def delete_api_key(key_id: str, _: str = Depends(require_basic)) -> None:
    if not _api_keys.revoke(key_id):
        raise HTTPException(status_code=404, detail={"error": "not_found", "code": 404})


def _format_sse(event_id: int, payload: dict[str, Any]) -> str:
    return (
        f"id: {event_id}\n"
        f"event: {payload.get('type', 'message')}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n"
        "\n"
    )


@app.get("/api/sessions/{sid}/events")
async def session_events(
    request: Request,
    sid: str,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _: str = Depends(require_basic),
) -> StreamingResponse:
    with _lock:
        _get(sid)
        conn_n = _sse_conn_counts.get(sid, 0) + 1
        _sse_conn_counts[sid] = conn_n

    try:
        last_id = int(last_event_id) if last_event_id else 0
    except ValueError:
        last_id = 0

    async def gen() -> Any:
        yield f"retry: {SSE_RETRY_MS}\n\n"
        yield ": keepalive\n\n"
        cursor = last_id
        emitted = 0
        last_keepalive = time.monotonic()
        while True:
            if await request.is_disconnected():
                return
            with _lock:
                sess = _sessions.get(sid)
                if sess is None:
                    return
                events = list(sess["events"])
            while cursor < len(events):
                if await request.is_disconnected():
                    return
                cursor += 1
                await asyncio.sleep(SSE_INTERVAL_S)
                yield _format_sse(cursor, events[cursor - 1])
                emitted += 1
                if SSE_DROP_FIRST_AFTER > 0 and conn_n == 1 and emitted >= SSE_DROP_FIRST_AFTER:
                    return
            now = time.monotonic()
            if now - last_keepalive >= SSE_KEEPALIVE_S:
                yield ": keepalive\n\n"
                last_keepalive = now
            await asyncio.sleep(min(0.1, SSE_INTERVAL_S))

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def reset_state() -> None:
    """Test helper: drop all in-memory sessions; reseed canned accounts."""
    with _lock:
        _sessions.clear()
        _sse_conn_counts.clear()
    for account in _accounts.list():
        _accounts.remove(account.id)
    seed_accounts()


def seed_accounts() -> None:
    """Canned accounts: one active account per provider (credentials absent)."""
    past = _now() - timedelta(hours=2)
    for provider in PROVIDER_MODELS:
        _accounts.put(
            Account(
                id=f"acct-{provider}-1",
                provider=provider,
                label=f"{provider} (mock)",
                max_concurrent=1,
                models=tuple(PROVIDER_MODELS[provider]),
                created_at=_iso(past),
            )
        )


def seed_demo_sessions() -> None:
    """Terminal sessions with canned history for the chat page's read-only e2e."""
    past = _now() - timedelta(minutes=12)
    canned = _enrich_turn(
        _load_fixture_events(),
        1,
        "I will write a small file in /work and verify it.",
    )
    usage = _usage_from_events(canned)
    user_msg = {
        "role": "user",
        "text": "Create hello.txt in the workspace.",
        "turn_id": "turn-1",
        "ts": _iso(past),
    }
    asst_msg = {
        "role": "assistant",
        "text": _assistant_text(canned),
        "turn_id": "turn-1",
        "ts": _iso(past + timedelta(seconds=8)),
    }
    specs = (
        ("seed-closed", "已关闭 · 只读历史", "closed", "codex"),
        ("seed-timeout", "已超时 · 只读历史", "timed_out", "devin"),
        ("seed-lost", "已丢失 · 只读历史", "lost", "grok"),
    )
    with _lock:
        for sid, title, status, provider in specs:
            _sessions[sid] = _new_session(
                sid=sid,
                title=title,
                model=PROVIDER_MODELS[provider][0],
                provider=provider,
                account_id=f"acct-{provider}-1",
                status=status,
                created_at=past,
                events=canned,
                messages=[user_msg, asst_msg],
                turns=1,
                usage=usage,
            )


def mount_web() -> None:
    if WEB_DIR.is_dir() and not any(getattr(r, "name", None) == "web" for r in app.routes):
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


mount_web()
seed_accounts()


def main() -> None:
    parser = argparse.ArgumentParser(description="sbx-browser mock session API")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="Do not insert closed/timed_out/lost demo sessions",
    )
    args = parser.parse_args()
    if not args.no_seed:
        seed_demo_sessions()
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

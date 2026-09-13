"""In-memory FastAPI mock of docs/contracts/api.yaml.

Start: ``python -m tests.fakes.mock_api --port 8787``

HTTP Basic user/password default to ``sbx`` / ``sbx`` (local mock only, not a secret).
Override with ``SBX_API_USER`` / ``SBX_API_PASSWORD``.
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "events" / "success.jsonl"
MAX_CONCURRENT = 2
SSE_INTERVAL_S = float(os.environ.get("SBX_SSE_INTERVAL_SECONDS", "0.2"))
SSE_KEEPALIVE_S = float(os.environ.get("SBX_SSE_KEEPALIVE_SECONDS", "15"))
BASIC_USER = os.environ.get("SBX_API_USER", "sbx")
BASIC_PASSWORD = os.environ.get("SBX_API_PASSWORD", "sbx")

app = FastAPI(title="sbx-browser mock API", version="0.1.0")
security = HTTPBasic(auto_error=False)
_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _usage() -> dict[str, int]:
    return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}


def _cost(usage: dict[str, int]) -> float:
    return round(
        usage["input_tokens"] * 1.25e-6
        + usage["cached_input_tokens"] * 0.125e-6
        + usage["output_tokens"] * 1.0e-5,
        6,
    )


def _load_fixture_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _public(sess: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    closed = sess["status"] in {"closed", "timed_out", "lost"}
    end = sess["updated_at"] if closed else now
    sandbox_seconds = max(0.0, (end - sess["created_at"]).total_seconds())
    return {
        "id": sess["id"],
        "title": sess["title"],
        "status": sess["status"],
        "created_at": _iso(sess["created_at"]),
        "updated_at": _iso(sess["updated_at"]),
        "model": sess["model"],
        "turns": sess["turns"],
        "usage": dict(sess["usage"]),
        "cost_estimate_usd": _cost(sess["usage"]),
        "sandbox_seconds": sandbox_seconds,
        "messages": list(sess["messages"]),
    }


def _error(code: int, error: str) -> JSONResponse:
    return JSONResponse(status_code=code, content={"error": error, "code": code})


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
    model: str | None = None


class PostMessageRequest(BaseModel):
    text: str = Field(min_length=1)


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
        sid = uuid.uuid4().hex
        now = _now()
        _sessions[sid] = {
            "id": sid,
            "title": body.title or "untitled",
            "status": "idle",
            "created_at": now,
            "updated_at": now,
            "model": body.model or "gpt-5",
            "turns": 0,
            "usage": _usage(),
            "messages": [],
            "events": _load_fixture_events(),
            "current_turn_id": None,
            "timer": None,
        }
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


def _finish_turn(sid: str, turn_id: str) -> None:
    time.sleep(1.0)
    with _lock:
        sess = _sessions.get(sid)
        if sess is None:
            return
        if sess["status"] != "running" or sess["current_turn_id"] != turn_id:
            return
        sess["status"] = "idle"
        sess["current_turn_id"] = None
        sess["turns"] += 1
        sess["updated_at"] = _now()
        sess["usage"] = {
            "input_tokens": sess["usage"]["input_tokens"] + 128,
            "cached_input_tokens": sess["usage"]["cached_input_tokens"],
            "output_tokens": sess["usage"]["output_tokens"] + 64,
        }
        sess["messages"].append(
            {
                "role": "assistant",
                "text": "mock turn complete",
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
        turn_id = f"turn-{sess['turns'] + 1}"
        sess["status"] = "running"
        sess["current_turn_id"] = turn_id
        sess["updated_at"] = _now()
        sess["messages"].append(
            {
                "role": "user",
                "text": body.text,
                "turn_id": turn_id,
                "ts": _iso(_now()),
            }
        )
        timer = threading.Thread(target=_finish_turn, args=(sid, turn_id), daemon=True)
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
            sess["updated_at"] = _now()
        return {"status": sess["status"]}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str, _: str = Depends(require_basic)) -> dict[str, Any]:
    with _lock:
        sess = _get(sid)
        sess["status"] = "closed"
        sess["current_turn_id"] = None
        sess["updated_at"] = _now()
        return _public(sess)


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
        sess = _get(sid)
        events = list(sess["events"])

    try:
        last_id = int(last_event_id) if last_event_id else 0
    except ValueError:
        last_id = 0

    async def gen() -> Any:
        yield ": keepalive\n\n"
        for index, payload in enumerate(events, start=1):
            if await request.is_disconnected():
                return
            if index <= last_id:
                continue
            await asyncio.sleep(SSE_INTERVAL_S)
            yield _format_sse(index, payload)
        while True:
            if await request.is_disconnected():
                return
            await asyncio.sleep(SSE_KEEPALIVE_S)
            yield ": keepalive\n\n"

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
    """Test helper: drop all in-memory sessions."""
    with _lock:
        _sessions.clear()


def main() -> None:
    parser = argparse.ArgumentParser(description="sbx-browser mock session API")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

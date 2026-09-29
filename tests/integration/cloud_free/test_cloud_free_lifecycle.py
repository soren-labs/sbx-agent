"""WP2-F items 1–5, 7–9: real runner lifecycle on LocalProcessBackend."""

from __future__ import annotations

import hashlib
import json
import stat
import threading
import time
from datetime import timedelta
from pathlib import Path

import httpx
from control.backend import LocalProcessBackend
from control.reaper import reap
from control.store import InMemoryStore
from fastapi.testclient import TestClient
from tests.integration.cloud_free.conftest import AUTH
from tests.integration.cloud_free.helpers import (
    cleanup_sessions,
    fixture_usage,
    load_turn,
    parse_jsonl,
    sandbox_root,
    start_server,
    stop_server,
    wait_session,
)

LEAK_CODEX = Path(__file__).resolve().parents[2] / "unit" / "runner" / "leak_codex.py"
LEAK_SENTINEL = "sk-THISLEAKEDVALUE12"


def test_item1_init_writes_config_and_agents_without_secrets(client: TestClient, live_env) -> None:
    """Create session → runner init writes config.toml / AGENTS.md; no credentials."""
    _app, _backend, store = live_env
    created = client.post(
        "/api/sessions", json={"title": "init", "model": "gpt-5.6-luna"}, auth=AUTH
    )
    assert created.status_code == 201
    sid = created.json()["session_id"]
    try:
        rec = wait_session(client, sid, status="idle")
        assert rec["model"] == "gpt-5.6-luna"
        root = sandbox_root(store, sid)

        config = (root / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert 'model = "gpt-5.6-luna"' in config
        assert 'approval_policy = "never"' in config
        assert 'sandbox_mode = "danger-full-access"' in config
        assert "CODEX_AUTH_JSON" in config

        agents = (root / "AGENTS.md").read_text(encoding="utf-8")
        assert "Sandbox AGENTS.md" in agents
        assert "/work" in agents
        assert "confirmation" in agents.lower()

        auth_path = root / ".codex" / "auth.json"
        assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
        # The credential directory itself is restricted (SOR-101).
        assert stat.S_IMODE((root / ".codex").stat().st_mode) == 0o700
        auth = json.loads(auth_path.read_text(encoding="utf-8"))
        tokens = auth.get("tokens") or {}
        # Compare digests, not values: if a host credential ever leaked into
        # auth.json, a failing assert must not dump the token (SOR-55).
        expected = hashlib.sha256(b"REDACTED").hexdigest()
        for key in ("access_token", "refresh_token", "id_token"):
            if key in tokens:
                got = hashlib.sha256(str(tokens[key]).encode("utf-8")).hexdigest()
                assert got == expected, f"{key} is not the REDACTED placeholder"

        session = json.loads((root / "session.json").read_text(encoding="utf-8"))
        assert session["codex_session_id"] is None
        assert session["turn"] == 0
        assert (root / "events.jsonl").read_text(encoding="utf-8") == ""
        assert (root / "inbox").is_dir()
        assert (root / "turns").is_dir()

        blob = "\n".join(
            (
                config,
                agents,
                (root / "events.jsonl").read_text(encoding="utf-8"),
                (root / "session.json").read_text(encoding="utf-8"),
            )
        )
        assert "sk-" not in blob
        assert "BEGIN PRIVATE" not in blob
    finally:
        cleanup_sessions(client, sid)


def test_item2_three_turns_sse_resume_files_and_usage(
    client: TestClient, live_env, monkeypatch
) -> None:
    """success → resume → resume: SSE events, hello.txt continuity, usage matches store."""
    app, _backend, store = live_env
    created = client.post("/api/sessions", json={"title": "three"}, auth=AUTH)
    assert created.status_code == 201
    sid = created.json()["session_id"]
    wait_session(client, sid, status="idle")
    root = sandbox_root(store, sid)
    try:
        scenarios = ("success", "resume", "resume")
        for index, scenario in enumerate(scenarios, start=1):
            monkeypatch.setenv("FAKE_CODEX_SCENARIO", scenario)
            posted = client.post(
                f"/api/sessions/{sid}/messages",
                json={"text": f"turn-{index}"},
                auth=AUTH,
            )
            assert posted.status_code == 202, posted.text
            session = wait_session(client, sid, status="idle", min_turns=index)
            turn_id = posted.json()["turn_id"]
            user = next(
                m for m in session["messages"] if m["turn_id"] == turn_id and m["role"] == "user"
            )
            assert user["text"] == f"turn-{index}"
            assert any(
                m["turn_id"] == turn_id and m["role"] == "assistant" for m in session["messages"]
            )

        hello = (root / "hello.txt").read_text(encoding="utf-8")
        assert "hello from fake_codex" in hello
        assert hello.count("resumed by fake_codex") == 2

        turns = [load_turn(root, n) for n in (1, 2, 3)]
        expected = fixture_usage("success")
        resume = fixture_usage("resume")
        assert turns[0]["usage"]["input_tokens"] == expected["input_tokens"]
        assert turns[1]["usage"]["input_tokens"] == resume["input_tokens"]
        assert turns[2]["usage"]["input_tokens"] == resume["input_tokens"]
        assert turns[0]["codex_session_id"] == turns[1]["codex_session_id"]
        assert turns[1]["codex_session_id"] == turns[2]["codex_session_id"]

        session = client.get(f"/api/sessions/{sid}", auth=AUTH).json()
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            summed = sum(int(t["usage"][key]) for t in turns)
            assert session["usage"][key] == summed
        assert session["turns"] == 3

        events = parse_jsonl(root / "events.jsonl")
        types = [e["type"] for e in events]
        assert types.count("sbx.turn_started") == 3
        assert types.count("sbx.turn_finished") == 3
        assert "thread.started" in types

        # Events must be reachable over SSE (same JSONL the contract tails).
        server, thread, base = start_server(app)
        try:
            with httpx.Client(base_url=base, timeout=10.0) as http:
                seen: list[str] = []
                ids: list[int] = []
                with http.stream("GET", f"/api/sessions/{sid}/events", auth=AUTH) as resp:
                    assert resp.status_code == 200
                    assert "text/event-stream" in resp.headers["content-type"]
                    deadline = time.monotonic() + 8
                    for line in resp.iter_lines():
                        if line.startswith("id:"):
                            ids.append(int(line.split(":", 1)[1].strip()))
                        if line.startswith("event:"):
                            seen.append(line.split(":", 1)[1].strip())
                        if seen.count("sbx.turn_finished") >= 3 and len(ids) >= 10:
                            break
                        if time.monotonic() > deadline:
                            break
                assert "sbx.turn_started" in seen
                assert "thread.started" in seen
                assert seen.count("sbx.turn_finished") >= 3
                assert ids == list(range(ids[0], ids[-1] + 1))
        finally:
            stop_server(server, thread)
    finally:
        cleanup_sessions(client, sid)


def test_item3_message_during_turn_is_409(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "hang")
    sid = client.post("/api/sessions", json={"title": "busy"}, auth=AUTH).json()["session_id"]
    wait_session(client, sid, status="idle")
    try:
        first = client.post(f"/api/sessions/{sid}/messages", json={"text": "hang"}, auth=AUTH)
        assert first.status_code == 202
        second = client.post(f"/api/sessions/{sid}/messages", json={"text": "again"}, auth=AUTH)
        assert second.status_code == 409
        assert second.json() == {"error": "turn_in_progress", "code": 409}
    finally:
        cleanup_sessions(client, sid)


def test_item4_hang_stop_returns_idle_within_5s(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "hang")
    sid = client.post("/api/sessions", json={"title": "stop"}, auth=AUTH).json()["session_id"]
    wait_session(client, sid, status="idle")
    try:
        posted = client.post(f"/api/sessions/{sid}/messages", json={"text": "hang"}, auth=AUTH)
        assert posted.status_code == 202
        busy = client.get(f"/api/sessions/{sid}", auth=AUTH).json()
        assert busy["status"] == "running"
        started = time.monotonic()
        stopped = client.post(f"/api/sessions/{sid}/stop", auth=AUTH)
        assert stopped.status_code == 202
        idle = wait_session(client, sid, status="idle", timeout=5.0)
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, elapsed
        assert idle["status"] == "idle"
        assert stopped.json()["status"] == "idle"
    finally:
        cleanup_sessions(client, sid)


def test_item4_hang_soft_timeout_emits_turn_finished_timeout(
    client: TestClient, live_env, monkeypatch
) -> None:
    app, _backend, store = live_env
    app.state.plane.turn_max_seconds = 2
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "hang")
    sid = client.post("/api/sessions", json={"title": "timeout"}, auth=AUTH).json()["session_id"]
    wait_session(client, sid, status="idle")
    try:
        posted = client.post(f"/api/sessions/{sid}/messages", json={"text": "hang"}, auth=AUTH)
        assert posted.status_code == 202
        idle = wait_session(client, sid, status="idle", timeout=15.0)
        assert idle["status"] == "idle"
        events = parse_jsonl(sandbox_root(store, sid) / "events.jsonl")
        finished = [e for e in events if e.get("type") == "sbx.turn_finished"]
        assert finished, events
        # Contract: sbx.turn_finished.status is timeout (not session timed_out).
        assert finished[-1]["status"] == "timeout"
        assert finished[-1]["exit_code"] == 3
        turn = load_turn(sandbox_root(store, sid), 1)
        assert turn["status"] == "timeout"
        assert any(e.get("type") == "sbx.error" for e in events)
    finally:
        cleanup_sessions(client, sid)


def test_item5_nonzero_then_session_continues(client: TestClient, live_env, monkeypatch) -> None:
    _app, _backend, store = live_env
    sid = client.post("/api/sessions", json={"title": "nz"}, auth=AUTH).json()["session_id"]
    wait_session(client, sid, status="idle")
    try:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "nonzero")
        posted = client.post(f"/api/sessions/{sid}/messages", json={"text": "fail"}, auth=AUTH)
        assert posted.status_code == 202
        wait_session(client, sid, status="idle")
        events = parse_jsonl(sandbox_root(store, sid) / "events.jsonl")
        types = [e["type"] for e in events]
        assert "turn.failed" in types
        finished = [e for e in events if e.get("type") == "sbx.turn_finished"]
        assert finished[-1]["status"] == "codex_error"
        assert finished[-1]["exit_code"] == 2
        turn = load_turn(sandbox_root(store, sid), 1)
        assert turn["status"] == "codex_error"

        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
        again = client.post(f"/api/sessions/{sid}/messages", json={"text": "ok"}, auth=AUTH)
        assert again.status_code == 202
        session = wait_session(client, sid, status="idle", min_turns=2)
        assert session["status"] == "idle"
        assert load_turn(sandbox_root(store, sid), 2)["status"] == "success"
    finally:
        cleanup_sessions(client, sid)


def test_item5_badjson_then_session_continues(client: TestClient, live_env, monkeypatch) -> None:
    _app, _backend, store = live_env
    sid = client.post("/api/sessions", json={"title": "bj"}, auth=AUTH).json()["session_id"]
    wait_session(client, sid, status="idle")
    try:
        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "badjson")
        posted = client.post(f"/api/sessions/{sid}/messages", json={"text": "bad"}, auth=AUTH)
        assert posted.status_code == 202
        wait_session(client, sid, status="idle")
        root = sandbox_root(store, sid)
        events = parse_jsonl(root / "events.jsonl")
        assert any(e.get("type") == "sbx.error" for e in events)
        finished = [e for e in events if e.get("type") == "sbx.turn_finished"]
        assert finished[-1]["status"] == "bad_json"
        assert finished[-1]["exit_code"] == 4
        # Parser continues after the bad line.
        assert any(e.get("type") == "turn.completed" for e in events)
        turn = load_turn(root, 1)
        assert turn["status"] == "bad_json"
        assert turn["bad_json_lines"] >= 1

        monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
        again = client.post(f"/api/sessions/{sid}/messages", json={"text": "ok"}, auth=AUTH)
        assert again.status_code == 202
        session = wait_session(client, sid, status="idle", min_turns=2)
        assert session["status"] == "idle"
        assert (root / "hello.txt").is_file()
    finally:
        cleanup_sessions(client, sid)


def test_item7_two_sessions_isolated_third_is_429(
    client: TestClient, live_env, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
    _app, _backend, store = live_env
    # Pin the plane cap — the default now resolves SBX_MAX_CONCURRENT → 8
    # (SOR-271 round-4); this spec exercises cap enforcement itself.
    _app.state.plane.max_concurrent = 2
    a = client.post("/api/sessions", json={"title": "a"}, auth=AUTH)
    b = client.post("/api/sessions", json={"title": "b"}, auth=AUTH)
    assert a.status_code == 201
    assert b.status_code == 201
    sid_a = a.json()["session_id"]
    sid_b = b.json()["session_id"]
    try:
        wait_session(client, sid_a, status="idle")
        wait_session(client, sid_b, status="idle")
        c = client.post("/api/sessions", json={"title": "c"}, auth=AUTH)
        assert c.status_code == 429
        assert c.json() == {"error": "concurrency_limit", "code": 429}

        errors: list[BaseException] = []

        def _turn(sid: str, text: str) -> None:
            try:
                posted = client.post(
                    f"/api/sessions/{sid}/messages", json={"text": text}, auth=AUTH
                )
                assert posted.status_code == 202, posted.text
                wait_session(client, sid, status="idle", min_turns=1)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        t_a = threading.Thread(target=_turn, args=(sid_a, "alpha"), daemon=True)
        t_b = threading.Thread(target=_turn, args=(sid_b, "beta"), daemon=True)
        t_a.start()
        t_b.start()
        t_a.join(timeout=20)
        t_b.join(timeout=20)
        assert not t_a.is_alive() and not t_b.is_alive()
        assert errors == []

        root_a = sandbox_root(store, sid_a)
        root_b = sandbox_root(store, sid_b)
        assert root_a != root_b
        assert (root_a / "hello.txt").read_text(encoding="utf-8") == "hello from fake_codex\n"
        assert (root_b / "hello.txt").read_text(encoding="utf-8") == "hello from fake_codex\n"
        inbox_a = (root_a / "inbox" / "1.md").read_text(encoding="utf-8")
        inbox_b = (root_b / "inbox" / "1.md").read_text(encoding="utf-8")
        assert "alpha" in inbox_a
        assert "beta" in inbox_b
        assert "beta" not in inbox_a
        assert "alpha" not in inbox_b
        sess_a = client.get(f"/api/sessions/{sid_a}", auth=AUTH).json()
        sess_b = client.get(f"/api/sessions/{sid_b}", auth=AUTH).json()
        assert sess_a["turns"] == 1
        assert sess_b["turns"] == 1
        assert sess_a["id"] != sess_b["id"]
    finally:
        cleanup_sessions(client, sid_a, sid_b)


def test_item8_reaper_idle_orphan_and_closed_history(client: TestClient, live_env) -> None:
    _app, backend, store = live_env
    assert isinstance(backend, LocalProcessBackend)
    assert isinstance(store, InMemoryStore)

    sid = client.post("/api/sessions", json={"title": "idle-reap"}, auth=AUTH).json()["session_id"]
    wait_session(client, sid, status="idle")
    rec = store.get(sid)
    assert rec is not None
    handle = rec.handle()
    assert handle is not None
    later = rec.last_activity_at + timedelta(minutes=31)
    actions = reap(store, backend, later, idle_timeout_s=1800)
    timed = store.get(sid)
    assert timed is not None
    assert timed.status == "timed_out"
    assert backend.poll(handle).alive is False
    assert any(a.kind == "timed_out" and a.session_id == sid for a in actions)
    replay = client.get(f"/api/sessions/{sid}", auth=AUTH)
    assert replay.status_code == 200
    assert replay.json()["status"] == "timed_out"

    orphan_sid = client.post("/api/sessions", json={"title": "orphan"}, auth=AUTH).json()[
        "session_id"
    ]
    wait_session(client, orphan_sid, status="idle")
    orphan_rec = store.get(orphan_sid)
    assert orphan_rec is not None
    orphan_handle = orphan_rec.handle()
    assert orphan_handle is not None
    store.delete(orphan_sid)
    assert store.get(orphan_sid) is None
    orphan_actions = reap(store, backend, later, idle_timeout_s=1800)
    assert backend.poll(orphan_handle).alive is False
    assert any(
        a.kind == "orphan_terminate" and a.sandbox_id == orphan_handle.id for a in orphan_actions
    )

    hist_sid = client.post("/api/sessions", json={"title": "hist"}, auth=AUTH).json()["session_id"]
    wait_session(client, hist_sid, status="idle")
    posted = client.post(
        f"/api/sessions/{hist_sid}/messages", json={"text": "remember me"}, auth=AUTH
    )
    assert posted.status_code == 202
    wait_session(client, hist_sid, status="idle", min_turns=1)
    closed = client.delete(f"/api/sessions/{hist_sid}", auth=AUTH)
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    hist = client.get(f"/api/sessions/{hist_sid}", auth=AUTH).json()
    assert hist["status"] == "closed"
    assert hist["turns"] == 1
    assert any(m["text"] == "remember me" for m in hist["messages"] if m["role"] == "user")
    assert any(m["role"] == "assistant" for m in hist["messages"])
    denied = client.post(f"/api/sessions/{hist_sid}/messages", json={"text": "nope"}, auth=AUTH)
    assert denied.status_code == 409
    listed = client.get("/api/sessions", auth=AUTH).json()
    assert any(item["id"] == hist_sid and item["status"] == "closed" for item in listed)


def test_item9_fake_secret_redacted_from_archive_and_logs(
    client: TestClient, live_env, monkeypatch
) -> None:
    _app, _backend, store = live_env
    monkeypatch.setenv("CODEX_BIN", str(LEAK_CODEX))
    sid = client.post("/api/sessions", json={"title": "redact"}, auth=AUTH).json()["session_id"]
    wait_session(client, sid, status="idle")
    try:
        posted = client.post(
            f"/api/sessions/{sid}/messages", json={"text": "do not echo secrets"}, auth=AUTH
        )
        assert posted.status_code == 202
        wait_session(client, sid, status="idle", min_turns=1)
        root = sandbox_root(store, sid)
        events_text = (root / "events.jsonl").read_text(encoding="utf-8")
        assert LEAK_SENTINEL not in events_text
        parsed = parse_jsonl(root / "events.jsonl")
        items = [
            e["item"]
            for e in parsed
            if e.get("type") == "item.completed" and isinstance(e.get("item"), dict)
        ]
        assert items
        assert items[0].get("api_key") == "REDACTED"
        turn = load_turn(root, 1)
        assert LEAK_SENTINEL not in json.dumps(turn)
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            assert LEAK_SENTINEL not in text, path
    finally:
        cleanup_sessions(client, sid)

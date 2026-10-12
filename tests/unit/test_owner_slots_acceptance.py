"""Owner Slot acceptance client against an in-memory API: no network, no paid Turn.

Proves the request allowlist, the refusal rules and the cleanup paths before the
client is ever pointed at real subscription Slots.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.e2e_staging import owner_slots

URL = "https://api-staging.example.test"
SLOTS = [
    ("slot_c1", "codex", "Codex 1"),
    ("slot_g1", "grok", "Grok Build 1"),
    ("slot_g2", "grok", "Grok Build 2"),
    ("slot_a1", "antigravity", "Antigravity 1"),
    ("slot_a2", "antigravity", "Antigravity 2"),
]


class FakeApi:
    def __init__(self, *, scopes: list[str] | None = None, fail_provider: str | None = None):
        self.scopes = scopes or ["write"]
        self.fail_provider = fail_provider
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []
        self.sessions: dict[str, dict[str, Any]] = {}
        self.turns: dict[str, dict[str, Any]] = {}
        self.busy: set[str] = set()
        self.slots = {
            slot_id: {
                "id": slot_id,
                "provider": provider,
                "label": label,
                "state": "ready",
                "verified_at": "2026-10-12T10:00:00+00:00",
                "volume": {"name": f"vol-{slot_id}"},
                "capabilities": {
                    "catalog": {
                        "default_model": f"{provider}-default",
                        "models": [
                            {"id": f"{provider}-other", "reasoning": {"efforts": []}},
                            {
                                "id": f"{provider}-default",
                                "reasoning": {
                                    "efforts": [{"id": "low"}, {"id": "high"}],
                                    "default": "high",
                                },
                            },
                        ],
                    }
                },
            }
            for slot_id, provider, label in SLOTS
        }

    def slot_view(self, slot_id: str) -> dict[str, Any]:
        slot = self.slots[slot_id]
        holder = next(
            (s for s in self.sessions.values() if s["slot"] == slot_id and s["lease"] == "ready"),
            None,
        )
        busy = holder is not None or slot_id in self.busy
        worker = (
            {"lease_id": f"lease_{holder['id']}", "session_id": holder["id"]} if holder else None
        )
        return {**slot, "busy": busy, "status": "running" if busy else "ready", "worker": worker}

    def new_turn(self, session: dict[str, Any], content: str) -> str:
        turn_id = f"turn_{len(self.turns) + 1}"
        failing = self.slots[session["slot"]]["provider"] == self.fail_provider
        marker = re.search(r"SLOT-OK-[0-9A-F]{8}", content)
        session["marker"] = marker.group(0) if marker else session.get("marker")
        self.turns[turn_id] = {
            "id": turn_id,
            "session_id": session["id"],
            "polls": 0,
            "final": "failed" if failing else "succeeded",
            "reply": session["marker"],
        }
        session["turn_ids"].append(turn_id)
        return turn_id

    def turn_view(self, turn_id: str, *, advance: bool) -> dict[str, Any]:
        turn = self.turns[turn_id]
        if advance:
            turn["polls"] += 1
        # Several polls in "running" let sibling Slots be observed at the same time.
        done = turn["polls"] > 4
        return {
            "id": turn_id,
            "state": turn["final"] if done else "running",
            "reason": None,
            "usage": {"input_tokens": 12, "output_tokens": 5} if done else None,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            return self._handle(request)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        assert request.headers["authorization"] == "Bearer REDACTED-owner-key"

        def ok(body: Any, status: int = 200) -> httpx.Response:
            return httpx.Response(status, json=body)

        if path == "/api/me":
            return ok(
                {
                    "user": {"id": "user_owner", "email": "owner@example.test"},
                    "workspaces": [{"id": "ws_1"}],
                    "auth": {"via": "api_key", "scopes": self.scopes},
                }
            )
        if path == "/api/subscription-providers":
            return ok(
                {
                    "items": [
                        {"provider_id": p, "harness_provider_id": p}
                        for p in ("codex", "grok", "antigravity")
                    ]
                }
            )
        if path == "/api/workspaces/ws_1/machine-slots":
            return ok({"items": [self.slot_view(s) for s in self.slots]})
        if m := re.fullmatch(r"/api/machine-slots/(\w+)", path):
            return ok(self.slot_view(m.group(1)))
        if path == "/api/workspaces/ws_1/sessions" and method == "POST":
            body = json.loads(request.content)
            sid = f"ses_{len(self.sessions) + 1}"
            session = {
                "id": sid,
                "title": body["title"],
                "slot": body["inference"]["machine_slot_id"],
                "lifecycle": "open",
                "lease": "ready",
                "turn_ids": [],
                "body": body,
            }
            self.sessions[sid] = session
            turn_id = self.new_turn(session, body["message"]["content"])
            return ok({"session_id": sid, "turn_id": turn_id}, 202)
        if path == "/api/workspaces/ws_1/sessions":
            return ok(
                {
                    "items": [
                        {"id": s["id"], "title": s["title"], "lifecycle": s["lifecycle"]}
                        for s in self.sessions.values()
                    ]
                }
            )
        if m := re.fullmatch(r"/api/turns/(\w+)", path):
            return ok({"turn": self.turn_view(m.group(1), advance=True)})
        if m := re.fullmatch(r"/api/sessions/(\w+)(/[\w/]+)?", path):
            session, tail = self.sessions[m.group(1)], m.group(2) or ""
            if tail == "":
                return ok(
                    {
                        "session": {
                            "id": session["id"],
                            "lifecycle": session["lifecycle"],
                            "inference": {
                                "mode": "subscription",
                                "machine_slot_id": session["slot"],
                            },
                        }
                    }
                )
            if tail == "/events":
                items = [
                    {
                        "seq": 1,
                        "type": "executor.bound",
                        "payload": {"backend": "modal", "lease_id": f"lease_{session['id']}"},
                    }
                ]
                after = int(request.url.params.get("after", 0))
                return ok({"items": items[after:], "next_after": len(items)})
            if tail == "/executor":
                return ok({"backend": "modal", "leases": [{"state": session["lease"]}]})
            if tail == "/turns":
                return ok(
                    {"items": [self.turn_view(t, advance=False) for t in session["turn_ids"]]}
                )
            if tail == "/messages" and method == "GET":
                return ok(
                    {
                        "items": [
                            {
                                "turn_id": t,
                                "role": "assistant",
                                "parts": [{"kind": "text", "content": self.turns[t]["reply"]}],
                            }
                            for t in session["turn_ids"]
                        ]
                    }
                )
            if tail == "/messages":
                content = json.loads(request.content)["content"]
                return ok({"turn_id": self.new_turn(session, content)}, 202)
            if tail == "/executor/releases":
                session["lease"] = "released"
                return ok({}, 202)
            if tail == "/closures":
                session["lifecycle"] = "closed"
                return ok({})
        if m := re.fullmatch(r"/api/turns/(\w+)/cancellations", path):
            self.turns[m.group(1)]["polls"] = 99
            self.turns[m.group(1)]["final"] = "cancelled"
            return ok({})
        raise AssertionError(f"unexpected request {method} {path}")


@pytest.fixture
def key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "owner_api_key"
    path.write_text("REDACTED-owner-key\n")
    path.chmod(0o600)
    monkeypatch.setenv("SBX_STAGING_URL", URL)
    monkeypatch.setenv("SBX_STAGING_OWNER_KEY_FILE", str(path))
    monkeypatch.setenv("SBX_STAGING_EVIDENCE", str(tmp_path / "evidence.json"))
    real_sleep = owner_slots.time.sleep
    # Polls become a brief yield, so sibling threads interleave as they do in a real run.
    monkeypatch.setattr(owner_slots.time, "sleep", lambda _s: real_sleep(0.002))
    return path


def run(api: FakeApi, *argv: str) -> int:
    return owner_slots.main(list(argv), transport=httpx.MockTransport(api.handle))


def evidence(key_file: Path) -> dict[str, Any]:
    return json.loads((key_file.parent / "evidence.json").read_text())


def mutations(api: FakeApi) -> list[tuple[str, str]]:
    return [r for r in api.requests if r[0] != "GET"]


def test_preflight_is_read_only(key_file: Path) -> None:
    api = FakeApi()
    assert run(api) == 0
    assert mutations(api) == []
    assert [s["label"] for s in evidence(key_file)["inventory"]] == sorted(
        (label for _, _, label in SLOTS), key=lambda x: x
    )


def test_run_exercises_every_slot_and_leaves_them_untouched(key_file: Path) -> None:
    api = FakeApi()
    assert run(api, "--run") == 0
    report = evidence(key_file)
    assert {s["label"]: s["result"] for s in report["slots"]} == {
        label: "PASS" for _, _, label in SLOTS
    }
    assert report["cleanup"] == {"leaked_sessions": [], "slots_changed": []}
    assert "REDACTED-owner-key" not in json.dumps(report)
    # One Session per Slot, each with the catalog's default model and that model's default effort.
    assert sorted(s["slot"] for s in api.sessions.values()) == sorted(s for s, _, _ in SLOTS)
    for session in api.sessions.values():
        provider = api.slots[session["slot"]]["provider"]
        assert session["body"]["harness"] == {
            "provider_id": provider,
            "model": f"{provider}-default",
            "effort": "high",
        }
        assert session["lifecycle"] == "closed" and session["lease"] == "released"
    pairs = [s for s in report["slots"] if s["provider"] in ("grok", "antigravity")]
    assert all(s["checks_group"]["ran_concurrently_with_sibling_slot"] for s in pairs)
    # Nothing but the allowlisted Session operations ever left the client.
    assert all(
        method == "POST"
        and any(p.match(path) for p in owner_slots.ALLOWED_MUTATIONS)
        and "machine-slots" not in path
        for method, path in mutations(api)
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/api/machine-slots/slot_c1"),
        ("PATCH", "/api/machine-slots/slot_c1"),
        ("POST", "/api/machine-slots/slot_c1/logout"),
        ("POST", "/api/machine-slots/slot_c1/logins"),
        ("POST", "/api/machine-slots/slot_c1/verifications"),
        ("POST", "/api/workspaces/ws_1/machine-slots"),
        ("DELETE", "/api/connections/conn_1"),
        ("POST", "/api/api-keys"),
        ("POST", "/api/auth/password-changes"),
    ],
)
def test_guard_refuses_everything_that_could_change_a_slot(method: str, path: str) -> None:
    api = FakeApi()
    client = owner_slots.owner_client(URL, "REDACTED-owner-key", httpx.MockTransport(api.handle))
    with pytest.raises(owner_slots.Refused):
        client.http.request(method, path)
    assert api.requests == []


def test_full_scope_key_is_refused_by_default(key_file: Path) -> None:
    api = FakeApi(scopes=["*"])
    with pytest.raises(SystemExit, match="scope"):
        run(api, "--run")
    assert mutations(api) == []
    assert run(FakeApi(scopes=["*"]), "--allow-full-scope") == 0


def test_busy_slot_stops_the_run_before_any_turn(key_file: Path) -> None:
    api = FakeApi()
    api.busy.add("slot_g2")
    assert run(api, "--run") == 1
    assert mutations(api) == []
    assert any("Grok Build 2" in p for p in evidence(key_file)["inventory_problems"])


def test_missing_slot_fails_the_inventory(key_file: Path) -> None:
    api = FakeApi()
    del api.slots["slot_a2"]
    assert run(api, "--run") == 1
    assert mutations(api) == []


def test_failed_turn_still_releases_and_closes(key_file: Path) -> None:
    api = FakeApi(fail_provider="grok")
    assert run(api, "--run") == 1
    report = evidence(key_file)
    results = {s["label"]: s["result"] for s in report["slots"]}
    assert results["Grok Build 1"] == results["Grok Build 2"] == "FAIL"
    assert results["Codex 1"] == results["Antigravity 1"] == "PASS"
    assert report["cleanup"] == {"leaked_sessions": [], "slots_changed": []}
    assert all(
        s["lifecycle"] == "closed" and s["lease"] == "released" for s in api.sessions.values()
    )


def test_key_file_must_be_private(key_file: Path) -> None:
    key_file.chmod(0o644)
    with pytest.raises(SystemExit, match="readable"):
        run(FakeApi())

"""Owner-run acceptance of existing, signed-in subscription Machine Slots on staging.

Runs one tiny real Turn (plus a short follow-up) on every Slot the owner already
has, through the public staging API, authenticated with an API key the owner
created. Slots of the same provider run at the same time.

Safety properties, enforced in code:

- The key is read from a private file (mode 0600), never from argv or the
  environment, and is never printed or written to the evidence. A key with
  the ``*`` scope is refused unless ``--allow-full-scope`` is given; a
  ``write`` key cannot change the password or mint further keys.
- Every request goes through an allowlist. Besides reads, the only mutations
  that can leave this process are: create a Session, send a message, cancel a
  Turn, release a Session's executor, close a Session. A request that would
  create, change, verify, sign out, re-login or delete a Machine Slot, touch a
  Connection or mint a key is refused before it reaches the network.
- A Slot that is busy or not ready is skipped and reported, never preempted.
- Sessions created here (title prefix ``owner-slot-acceptance``) are released
  and closed on success, failure and interruption.

Default mode is a read-only preflight. ``--run`` starts the paid Turns.

Environment:
  SBX_STAGING_URL             staging API origin (production hosts are refused)
  SBX_STAGING_OWNER_KEY_FILE  default ``~/.config/sbx-staging/owner_api_key``
  SBX_STAGING_OWNER_EXPECT    default ``codex=1,grok=2,antigravity=2``
  SBX_STAGING_EVIDENCE        optional path for the evidence JSON
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import httpx

from tests.e2e_staging.common import SBXClient, SBXError, staging_url

PREFIX = "owner-slot-acceptance"
DEFAULT_KEY_FILE = "~/.config/sbx-staging/owner_api_key"
DEFAULT_EXPECT = "codex=1,grok=2,antigravity=2"
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")
RELEASED = ("released", "lost")
_ID = r"[A-Za-z0-9_\-]+"
ALLOWED_MUTATIONS = tuple(
    re.compile(pattern)
    for pattern in (
        rf"^/api/workspaces/{_ID}/sessions$",
        rf"^/api/sessions/{_ID}/messages$",
        rf"^/api/sessions/{_ID}/executor/releases$",
        rf"^/api/sessions/{_ID}/closures$",
        rf"^/api/turns/{_ID}/cancellations$",
    )
)


class Refused(RuntimeError):
    """A request outside the allowlist; raised before any network traffic."""


class GuardedClient(httpx.Client):
    def request(self, method: str, url: Any, **kw: Any) -> httpx.Response:  # type: ignore[override]
        path = httpx.URL(str(url)).path
        if method.upper() not in ("GET", "HEAD") and not (
            method.upper() == "POST" and any(p.match(path) for p in ALLOWED_MUTATIONS)
        ):
            raise Refused(f"{method.upper()} {path} is not permitted in owner Slot acceptance")
        return super().request(method, url, **kw)


def read_key(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(
            f"owner API key file {path} not found. The owner creates a key in the staging "
            "Console (Settings, API keys) and saves it there with mode 0600."
        )
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SystemExit(f"{path} must not be readable by group or others (mode is {mode:o})")
    key = path.read_text().strip()
    if not key:
        raise SystemExit(f"{path} is empty")
    return key


def owner_client(url: str, key: str, transport: httpx.BaseTransport | None = None) -> SBXClient:
    http = GuardedClient(base_url=url, timeout=120, transport=transport)
    return SBXClient(url, api_key=key, http=http)


def parse_expect(text: str) -> dict[str, int]:
    return {k.strip(): int(v) for k, v in (item.split("=") for item in text.split(",") if item)}


def fingerprint(slot: dict[str, Any]) -> dict[str, Any]:
    """What must be identical before and after: the Slot record and its login Volume."""
    return {
        "id": slot["id"],
        "provider": slot["provider"],
        "label": slot["label"],
        "state": slot["state"],
        "volume": (slot.get("volume") or {}).get("name"),
    }


def choose_model(slot: dict[str, Any]) -> tuple[str | None, str | None]:
    """The catalog's own default model and that model's default (else lowest) effort."""
    catalog = (slot.get("capabilities") or {}).get("catalog") or {}
    models = catalog.get("models") or []
    if not models:
        return None, None
    wanted = catalog.get("default_model")
    model = (
        next((m for m in models if m["id"] == wanted), None)
        or next((m for m in models if m.get("default")), None)
        or models[0]
    )
    reasoning = model.get("reasoning") or {}
    efforts = [e["id"] for e in reasoning.get("efforts") or []]
    effort = reasoning.get("default") if reasoning.get("default") in efforts else None
    return model["id"], effort or (efforts[0] if efforts else None)


def wait(pred: Any, timeout: float, poll: float = 3.0) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(poll)
    return None


def reply_text(c: SBXClient, session_id: str, turn_id: str) -> str:
    return "\n".join(
        part["content"] or ""
        for m in c.messages.list(session_id)
        if m["turn_id"] == turn_id and m["role"] == "assistant"
        for part in m["parts"]
        if part["kind"] == "text"
    )


def lease_states(c: SBXClient, session_id: str) -> list[str]:
    return [lease["state"] for lease in c.sessions.executor(session_id)["leases"]]


def finish_session(c: SBXClient, session_id: str) -> bool:
    """Stop work, release the executor, close. True when no lease is left live."""
    try:
        for turn in c.turns.list(session_id):
            if turn["state"] not in TERMINAL:
                c.turns.cancel(turn["id"])
        if any(state not in RELEASED for state in lease_states(c, session_id)):
            c.sessions.release(session_id)
            wait(lambda: all(s in RELEASED for s in lease_states(c, session_id)), 240)
        if c.sessions.get(session_id)["session"]["lifecycle"] == "open":
            c.sessions.close(session_id)
    except SBXError as exc:
        print(f"[warn] cleanup of {session_id}: {exc.code}", flush=True)
    return all(state in RELEASED for state in lease_states(c, session_id))


def exercise(
    c: SBXClient,
    slot: dict[str, Any],
    harness: str,
    out: dict[str, Any],
    together: threading.Barrier,
) -> None:
    """One Slot: Turn with a marker, follow-up in the same Session, release, close."""
    checks: dict[str, Any] = out["checks"]
    started = time.monotonic()
    model, effort = choose_model(slot)
    out.update(model=model, effort=effort)
    marker = f"SLOT-OK-{secrets.token_hex(4).upper()}"
    session_id = None
    try:
        created = c.sessions.create(
            title=f"{PREFIX} {slot['label']} {time.strftime('%Y-%m-%d %H:%M:%S')}",
            harness={
                "provider_id": harness,
                **({"model": model} if model else {}),
                **({"effort": effort} if effort else {}),
            },
            executor={"backend": "modal", "resource_class": "small"},
            inference={"mode": "subscription", "machine_slot_id": slot["id"]},
            message={"content": f"Reply with exactly this text and nothing else: {marker}"},
        )
        session_id, turn_id = created["session_id"], created["turn_id"]
        out["session_id"] = session_id
        try:
            # Sibling Slots start watching only once each has its own Session.
            together.wait(timeout=120)
        except threading.BrokenBarrierError:
            pass
        pinned = c.sessions.get(session_id)["session"]
        checks["session_pinned_to_slot"] = (
            pinned["inference"]["mode"] == "subscription"
            and pinned["inference"]["machine_slot_id"] == slot["id"]
        )

        seen_worker: dict[str, Any] = {}

        def terminal() -> Any:
            view = c.slots.get(slot["id"])
            worker = view.get("worker")
            if worker and worker.get("session_id") == session_id:
                seen_worker.update(worker, status=view["status"])
                out.setdefault("running_from", time.time())
                out["running_until"] = time.time()
            turn = c.turns.get(turn_id)
            return turn if turn["state"] in TERMINAL else None

        turn = wait(terminal, 900, poll=2.0)
        checks["turn_succeeded"] = bool(turn) and turn["state"] == "succeeded"
        out["turn_state"] = turn["state"] if turn else "timeout"
        events = list(c.sessions.events(session_id))
        bound = next((e for e in events if e["type"] == "executor.bound"), None)
        checks["executed_on_modal_vm"] = (
            bool(bound)
            and bound["payload"]["backend"] == "modal"
            and c.sessions.executor(session_id)["backend"] == "modal"
        )
        checks["slot_held_by_this_session_lease"] = bool(seen_worker) and (
            not bound or seen_worker.get("lease_id") == bound["payload"]["lease_id"]
        )
        usage = (turn or {}).get("usage") or {}
        out["usage"] = {k: v for k, v in usage.items() if isinstance(v, int | float)}
        checks["provider_reported_usage"] = bool(out["usage"])
        if not checks["turn_succeeded"]:
            return
        checks["reply_contains_marker"] = marker in reply_text(c, session_id, turn_id)

        follow_id = c.sessions.send(
            session_id, "Repeat the exact text I asked you to reply with. Nothing else."
        )["turn_id"]
        follow = wait(
            lambda: (t := c.turns.get(follow_id))["state"] in TERMINAL and t, 600, poll=2.0
        )
        checks["follow_up_turn_succeeded"] = bool(follow) and follow["state"] == "succeeded"
        # Remembering the marker shows the provider's own session continued.
        checks["follow_up_recalls_marker"] = bool(follow) and marker in reply_text(
            c, session_id, follow_id
        )
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {getattr(exc, 'code', '') or exc}"[:300]
    finally:
        if session_id:
            checks["lease_released_and_session_closed"] = finish_session(c, session_id)
        after = c.slots.get(slot["id"])
        checks["slot_free_again"] = not after["busy"] and after["status"] == "ready"
        checks["slot_record_and_volume_unchanged"] = fingerprint(after) == fingerprint(slot)
        out["elapsed_seconds"] = round(time.monotonic() - started, 1)


# Model behavior rather than platform behavior: reported, but does not fail a Slot.
ADVISORY = frozenset({"reply_contains_marker", "follow_up_recalls_marker"})


def verdict(result: dict[str, Any]) -> str:
    required = {k: v for k, v in result["checks"].items() if k not in ADVISORY}
    expected = {
        "session_pinned_to_slot",
        "turn_succeeded",
        "executed_on_modal_vm",
        "slot_held_by_this_session_lease",
        "provider_reported_usage",
        "follow_up_turn_succeeded",
        "lease_released_and_session_closed",
        "slot_free_again",
        "slot_record_and_volume_unchanged",
    }
    return "PASS" if expected <= set(required) and all(required.values()) else "FAIL"


def inventory(c: SBXClient, expect: dict[str, int]) -> tuple[list[dict[str, Any]], list[str]]:
    slots = sorted(c.slots.list(), key=lambda s: (s["provider"], s["label"]))
    problems = []
    for provider, count in expect.items():
        found = [s for s in slots if s["provider"] == provider]
        if len(found) != count:
            problems.append(f"expected {count} {provider} Slot(s), found {len(found)}")
    for slot in slots:
        if slot["provider"] not in expect:
            continue
        if slot["status"] != "ready" or slot["state"] != "ready" or slot["busy"]:
            problems.append(f"{slot['label']}: status={slot['status']} busy={slot['busy']}")
        elif not slot.get("verified_at"):
            problems.append(f"{slot['label']}: ready but never verified")
    return [s for s in slots if s["provider"] in expect], problems


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(prog="owner-slot-acceptance")
    parser.add_argument("--run", action="store_true", help="start the real (paid) Turns")
    parser.add_argument(
        "--allow-full-scope",
        action="store_true",
        help="accept a key with the '*' scope (it could also change the password and mint keys)",
    )
    args = parser.parse_args(argv)
    url = staging_url()
    key_file = Path(os.environ.get("SBX_STAGING_OWNER_KEY_FILE", DEFAULT_KEY_FILE)).expanduser()
    c = owner_client(url, read_key(key_file), transport)
    expect = parse_expect(os.environ.get("SBX_STAGING_OWNER_EXPECT", DEFAULT_EXPECT))
    evidence: dict[str, Any] = {
        "target": url,
        "mode": "run" if args.run else "preflight",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": "Each Slot ran two short Turns on the owner's paid subscription.",
        "slots": [],
    }
    me = c.me()
    auth = me.get("auth") or {}
    evidence["principal"] = {
        "user_id": me["user"]["id"],
        "via": auth.get("via"),
        "scopes": auth.get("scopes"),
    }
    if auth.get("via") != "api_key":
        raise SystemExit("owner Slot acceptance must authenticate with an API key")
    if "*" in (auth.get("scopes") or []) and not args.allow_full_scope:
        raise SystemExit(
            "this API key has the '*' scope. Create one with scopes [\"write\"] "
            "(see docs/operations/staging.md) or pass --allow-full-scope."
        )
    harness_of = {p["provider_id"]: p["harness_provider_id"] for p in c.slots.providers()}
    slots, problems = inventory(c, expect)
    evidence["inventory"] = [
        {**fingerprint(s), "status": s["status"], "verified_at": s.get("verified_at")}
        for s in slots
    ]
    evidence["inventory_problems"] = problems
    for slot in slots:
        model, effort = choose_model(slot)
        print(
            f"[slot] {slot['provider']:<12} {slot['label']:<18} status={slot['status']} "
            f"busy={slot['busy']} model={model} effort={effort}",
            flush=True,
        )
    for problem in problems:
        print(f"[FAIL] inventory: {problem}", flush=True)

    code = 1 if problems else 0
    try:
        if args.run and not problems:
            for provider in sorted(expect):
                group = [s for s in slots if s["provider"] == provider]
                results = [
                    {"slot_id": s["id"], "label": s["label"], "provider": provider, "checks": {}}
                    for s in group
                ]
                together = threading.Barrier(len(group))
                threads = [
                    threading.Thread(
                        target=exercise,
                        args=(c, s, harness_of[provider], r, together),
                        daemon=True,
                    )
                    for s, r in zip(group, results, strict=True)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                if len(results) > 1:
                    # Both Slots were observed holding their own worker at the same time.
                    overlap = max(r.get("running_from", 1e18) for r in results) < min(
                        r.get("running_until", 0) for r in results
                    )
                    for r in results:
                        r["checks_group"] = {"ran_concurrently_with_sibling_slot": overlap}
                for r in results:
                    r["result"] = verdict(r)
                    r.pop("running_from", None), r.pop("running_until", None)
                    print(f"[{r['result']}] {provider} {r['label']} {json.dumps(r['checks'])}")
                    evidence["slots"].append(r)
            failed = [r["label"] for r in evidence["slots"] if r["result"] != "PASS"]
            code = 1 if failed or len(evidence["slots"]) != len(slots) else 0
    except BaseException:
        evidence["interrupted"] = traceback.format_exc(limit=2)
        code = 1
    finally:
        # Whatever happened above, nothing created here may keep a Slot or a VM.
        leaked = []
        for s in c.sessions.list()["items"]:
            if (s.get("title") or "").startswith(PREFIX) and s["lifecycle"] == "open":
                if not finish_session(c, s["id"]):
                    leaked.append(s["id"])
        final = {s["id"]: s for s in c.slots.list()}
        changed = [
            s["label"]
            for s in slots
            if s["id"] not in final or fingerprint(final[s["id"]]) != fingerprint(s)
        ]
        evidence["cleanup"] = {"leaked_sessions": leaked, "slots_changed": changed}
        if leaked or changed:
            code = 1
        print(f"[{'ok' if not (leaked or changed) else 'FAIL'}] cleanup {evidence['cleanup']}")
    evidence["result"] = (
        ("passed" if code == 0 else "failed")
        if args.run
        else "preflight " + ("ok" if code == 0 else "failed")
    )
    if path := os.environ.get("SBX_STAGING_EVIDENCE"):
        Path(path).write_text(json.dumps(evidence, indent=2, default=str))
    print(f"owner Slot acceptance: {evidence['result']}")
    return code


if __name__ == "__main__":
    sys.exit(main())

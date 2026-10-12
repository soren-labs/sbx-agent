"""External acceptance of the persistent staging deployment.

Signs in as the two stable test users and checks, over the public API only:
readiness, account isolation, stored Connection health, a real Turn on the
configured executor, lease release, and that data from earlier runs is still
there. Sessions it creates are always released and closed, also on failure.
Subscription Machine Slots that nobody has signed in to are reported as
``pending`` and never counted as passed.

Environment:
  SBX_STAGING_URL, SBX_STAGING_USER_{A,B}_EMAIL, SBX_STAGING_USER_{A,B}_PASSWORD
  SBX_STAGING_CONSOLE_URL     staging Console origin; its build SHA must equal the backend's
  SBX_STAGING_EXPECT_SHA      optional: the commit both must report
  SBX_STAGING_EXECUTOR        ``modal`` (default). ``local`` is a rehearsal of this
                              client against a fake-provider stack, not acceptance.
  SBX_STAGING_REQUIRE_SLOTS   comma-separated providers whose ready Slot is mandatory
  SBX_STAGING_EVIDENCE        optional path for the evidence JSON
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
import traceback
from typing import Any

import httpx

from tests.e2e_staging.common import (
    SESSION_PREFIX,
    SBXClient,
    SBXError,
    account,
    sign_in,
    staging_url,
)

EVIDENCE: dict[str, Any] = {"gates": {}, "pending": [], "notes": {}}


class GateFailed(Exception):
    pass


def gate(name: str, ok: Any, *, fatal: bool = True, **data: Any) -> None:
    EVIDENCE["gates"][name] = {"ok": bool(ok), **data}
    print(f"[{'ok' if ok else 'FAIL'}] {name} {json.dumps(data, default=str)[:240]}", flush=True)
    if not ok and fatal:
        raise GateFailed(name)


def wait(pred: Any, timeout: float, poll: float = 3.0) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(poll)
    return None


def denied(call: Any) -> bool:
    try:
        call()
    except SBXError as exc:
        return exc.status in (403, 404)
    return False


def lease_states(c: SBXClient, session_id: str) -> list[str]:
    return [lease["state"] for lease in c.sessions.executor(session_id)["leases"]]


def own_sessions(c: SBXClient) -> list[dict[str, Any]]:
    items = c.sessions.list()["items"]
    return [s for s in items if (s.get("title") or "").startswith(SESSION_PREFIX)]


def cleanup(c: SBXClient) -> list[str]:
    """Release and close every acceptance Session that is still open; return leaked ones."""
    leaked: list[str] = []
    for s in own_sessions(c):
        if s["lifecycle"] != "open":
            continue
        sid = s["id"]
        try:
            if any(state not in ("released", "lost") for state in lease_states(c, sid)):
                c.sessions.release(sid)
                wait(
                    lambda sid=sid: all(x in ("released", "lost") for x in lease_states(c, sid)),
                    180,
                )
            c.sessions.close(sid)
        except SBXError as exc:
            print(f"[warn] cleanup of {sid}: {exc.code}", flush=True)
        if any(state not in ("released", "lost") for state in lease_states(c, sid)):
            leaked.append(sid)
    return leaked


def run(url: str, backend: str) -> None:
    health = httpx.get(url + "/healthz", timeout=15).json()
    ready = httpx.get(url + "/readyz", timeout=15)
    gate("ready", ready.status_code == 200 and ready.json().get("ok") is True)
    expected = os.environ.get("SBX_STAGING_EXPECT_SHA")
    if backend == "modal":
        # A candidate is one commit: the backend image and the Console build must agree,
        # and match the requested commit when one is given. Never skipped on staging.
        console_url = os.environ.get("SBX_STAGING_CONSOLE_URL", "").rstrip("/")
        if not console_url:
            raise GateFailed("SBX_STAGING_CONSOLE_URL is required")
        console = httpx.get(console_url + "/build-manifest.json", timeout=15).json().get("git_sha")
        api = health.get("git_sha")
        gate(
            "revisions_aligned",
            bool(api) and api == console and expected in (None, "", api),
            backend=api,
            console=console,
            expected=expected,
        )

    a, b = sign_in(url, account("a")), sign_in(url, account("b"))
    gate("sign_in", a.me()["user"]["email_verified"] and b.me()["user"]["email_verified"])
    gate("distinct_workspaces", a.workspace_id != b.workspace_id)

    # Earlier runs must still be readable: staging keeps its database across rollouts.
    earlier = [s for s in own_sessions(a) if s["lifecycle"] != "open"]
    if earlier:
        messages = a.messages.list(earlier[0]["id"])
        gate("durable_history", len(messages) >= 2, sessions=len(earlier))
    else:
        EVIDENCE["notes"]["durable_history"] = "first run: no earlier acceptance Session yet"

    required = ["inference_api"] + (["modal"] if backend == "modal" else [])
    for name, c in (("a", a), ("b", b)):
        by_kind = {
            x["kind"]: x["health"] for x in c.connections.list() if x["state"] == "configured"
        }
        gate(
            f"connections_ready_{name}",
            all(by_kind.get(kind) == "ready" for kind in required),
            health={kind: by_kind.get(kind) for kind in required},
        )

    nonce = f"STAGING-OK-{secrets.token_hex(4).upper()}"
    # Rehearsal only: lets the fake CLI's directives (e.g. ``[fail]``) exercise failure paths.
    suffix = os.environ.get("SBX_STAGING_PROMPT_SUFFIX", "") if backend != "modal" else ""
    created = a.sessions.create(
        title=f"{SESSION_PREFIX} {time.strftime('%Y-%m-%d %H:%M:%S')}",
        harness={"provider_id": "opencode"},
        executor={
            "backend": backend,
            **({"resource_class": "small"} if backend == "modal" else {}),
        },
        message={"content": f"Reply with exactly this text and nothing else: {nonce}{suffix}"},
    )
    sid, tid = created["session_id"], created["turn_id"]
    gate(
        "isolation",
        denied(lambda: b.sessions.get(sid))
        and denied(lambda: b.turns.get(tid))
        and denied(lambda: b.get(f"/api/workspaces/{a.workspace_id}/connections")),
    )
    turn = a.turns.wait(tid, deadline=900, poll=5)
    gate("turn_succeeded", turn["state"] == "succeeded", state=turn["state"])
    text = "\n".join(
        part["content"]
        for m in a.messages.list(sid)
        if m["turn_id"] == tid and m["role"] == "assistant"
        for part in m["parts"]
        if part["kind"] == "text"
    )
    # With a real model this is instruction-following, not platform behavior.
    gate("reply_contains_nonce", nonce in text, fatal=False)

    view = a.sessions.executor(sid)
    gate("lease_on_backend", view["backend"] == backend and bool(view["leases"]))
    a.sessions.release(sid)
    released = wait(lambda: all(x in ("released", "lost") for x in lease_states(a, sid)), 180)
    gate("lease_released", released, states=lease_states(a, sid))
    a.sessions.close(sid)
    gate("session_closed", a.sessions.get(sid)["session"]["lifecycle"] != "open")

    slots = {}
    for slot in a.slots.list():
        slots.setdefault(slot["provider"], []).append(slot["status"])
    required_slots = [p for p in os.environ.get("SBX_STAGING_REQUIRE_SLOTS", "").split(",") if p]
    for provider in sorted({p["provider_id"] for p in a.slots.providers()} | set(required_slots)):
        if "ready" in slots.get(provider, []):
            EVIDENCE["notes"][f"slot_{provider}"] = (
                "ready (signed in); provider Turn not exercised here"
            )
        else:
            EVIDENCE["pending"].append(f"slot_{provider}: no signed-in Machine Slot")
            if provider in required_slots:
                gate(f"slot_{provider}", False, fatal=False)


def main() -> int:
    url = staging_url()
    backend = os.environ.get("SBX_STAGING_EXECUTOR", "modal")
    EVIDENCE["mode"] = "acceptance" if backend == "modal" else "rehearsal"
    failure = None
    try:
        run(url, backend)
    except GateFailed as exc:
        failure = f"gate failed: {exc}"
    except Exception:
        failure = traceback.format_exc(limit=3)
    finally:
        leaked: list[str] = []
        for name in ("a", "b"):
            try:
                leaked += cleanup(sign_in(url, account(name)))
            except Exception as exc:
                leaked.append(f"cleanup for user {name} did not run: {type(exc).__name__}")
        EVIDENCE["gates"]["no_leaked_leases"] = {"ok": not leaked, "sessions": leaked}
        print(f"[{'ok' if not leaked else 'FAIL'}] no_leaked_leases {leaked}", flush=True)
    failed = [name for name, g in EVIDENCE["gates"].items() if not g["ok"]]
    fatal = (
        failure is not None
        or "no_leaked_leases" in failed
        or any(name.startswith("slot_") for name in failed)
    )
    EVIDENCE["result"] = "failed" if fatal else "passed"
    if failure:
        EVIDENCE["failure"] = failure
        print(failure, file=sys.stderr)
    if path := os.environ.get("SBX_STAGING_EVIDENCE"):
        with open(path, "w") as f:
            json.dump(EVIDENCE, f, indent=2, default=str)
    for item in EVIDENCE["pending"]:
        print(f"[pending] {item}")
    label = (
        "staging acceptance" if EVIDENCE["mode"] == "acceptance" else "REHEARSAL (not acceptance)"
    )
    print(f"{label}: {EVIDENCE['result']}; non-fatal failures: {[n for n in failed if not fatal]}")
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())

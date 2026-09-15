"""SOR-63 acceptance gate: multi-account /v1 scheduling pool.

Validates the P2 Core scheduling contract end-to-end through the product
``/v1`` surface — the shape the Antigravity-4 / Grok-2 real pools must
satisfy on D1's persistent scheduler:

    fleet shape -> cooldown/failover -> auto rotation across accounts ->
    slot exhaustion -> named-account errors -> terminal runs ->
    reaper/close lease release -> no slot leak -> credential leak scan

Two modes, one check matrix:

* ``fake`` (default): in-process app (``LocalProcessBackend`` +
  ``stub_runner``) fronted by the real ``AccountScheduler`` over a
  ``PersistentAccountRegistry`` — the D1 implementation, same classes the
  Modal bootstrap installs. Runs the full matrix including injected
  ``report_failure`` cooldowns and reaper lease release. No credentials.
* ``real``: ``httpx`` against a deployed control plane seeded with
  ``SBX_<PROVIDER>_ACCOUNTS``. Explicitly opt-in (``--real`` or
  ``SBX_POOL_GATE_REAL=1``); requires an ``admin``-scoped bearer key for
  ``GET /v1/accounts`` fleet introspection. Cooldown injection and reaper
  internals are not reachable over the wire — those checks degrade to
  observation (a non-active account must refuse named picks) or are skipped
  with a recorded detail.

Nothing here fabricates a real-pool PASS: prerequisites missing → verdict
SKIP (exit 2). Credential material is never printed; only fingerprints and
account ids land in the artifact.

Run (from repo root):

    uv run python -m tests.acceptance.pool_gate                      # fake, antigravity x4
    uv run python -m tests.acceptance.pool_gate --provider grok      # fake, grok x2
    SBX_V1_API_KEY=... uv run python -m tests.acceptance.pool_gate \
        --provider antigravity --real --base-url https://...

Exit codes: 0 = PASS, 1 = FAIL, 2 = prerequisites missing (SKIP).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TERMINAL_RUN_STATUSES = ("FINISHED", "ERROR", "CANCELLED", "EXPIRED")
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
SK_RE = re.compile(r"sk-[A-Za-z0-9]{10,}")


@dataclass(frozen=True)
class GateSpec:
    """Per-provider fleet expectations for the SOR-63/D2 acceptance lane."""

    expected_accounts: int
    model: str


SPECS: dict[str, GateSpec] = {
    # The D2 acceptance lane: Antigravity 4 accounts first, then Grok 2.
    "antigravity": GateSpec(expected_accounts=4, model="gemini-3.8-flash-low"),
    "grok": GateSpec(expected_accounts=2, model="grok-4.6"),
    # codex is not part of the acceptance ask but stays available for a
    # cheap local sanity run (--provider codex).
    "codex": GateSpec(expected_accounts=2, model="gpt-5.6-luna"),
}

DEFAULT_PROMPT = "Reply with exactly this line and nothing else: POOL_GATE_OK"

CHECKS: list[dict[str, Any]] = []
RESULTS: dict[str, Any] = {}


def reset_state() -> None:
    """Clear module state so the gate can run more than once in-process."""
    CHECKS.clear()
    RESULTS.clear()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check(name: str, ok: bool, detail: str | None = None) -> bool:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})
    mark = "ok" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def record(key: str, value: object) -> None:
    RESULTS[key] = value


def write_json(name: str, payload: dict[str, Any]) -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS / name
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def leak_reason(text: str, secrets: list[str]) -> str | None:
    """Label a leaked secret in ``text``; never returns the secret itself."""
    for index, secret in enumerate(secrets):
        if secret and len(secret) >= 8 and secret in text:
            return f"credential#{index}"
    if JWT_RE.search(text):
        return "jwt"
    if SK_RE.search(text):
        return "sk-token"
    return None


# ---------------------------------------------------------------- transports


class FakeTransport:
    """In-process /v1 app over ``LocalProcessBackend`` + ``stub_runner``.

    The scheduler is the real SOR-63/D1 stack — ``AccountScheduler`` over a
    ``PersistentAccountRegistry`` (in-memory store) — the same classes the
    Modal bootstrap installs, so the gate exercises real atomic
    acquire/LRU/cooldown behaviour without a deployed control plane.
    A mutable clock lets the gate expire cooldowns deterministically.
    """

    mode = "fake"

    def __init__(
        self,
        provider: str,
        accounts: int,
        *,
        model: str,
        runner: Path,
        slots: int = 1,
    ) -> None:
        from control.accounts import InMemoryAccountStore, PersistentAccountRegistry
        from control.api_v1.state import InMemoryApiKeyStore
        from control.app import create_app
        from control.backend import LocalProcessBackend
        from control.ports import Account
        from control.run_store import InMemoryRunStore
        from control.scheduler import AccountScheduler
        from control.store import InMemoryStore
        from fastapi.testclient import TestClient

        self.provider = provider
        self.now = [datetime.now(UTC)]
        self.captured: list[str] = []

        self.backend = LocalProcessBackend()
        self.store = InMemoryStore()
        self.app = create_app(
            backend=self.backend,
            store=self.store,
            run_store=InMemoryRunStore(),
            runner_cmd=[sys.executable, str(runner)],
            clock=lambda: self.now[0],
            keepalive_s=0.2,
            # The global cap must not mask per-account scheduling: the gate
            # fills the fleet, so give the plane headroom above it.
            max_concurrent=accounts * slots + 4,
        )

        self.registry = PersistentAccountRegistry(InMemoryAccountStore())
        self.keys = InMemoryApiKeyStore()
        created_at = self.now[0].isoformat()
        for i in range(1, accounts + 1):
            account_id = f"{provider}-pool-{i}"
            self.registry.put(
                Account(
                    id=account_id,
                    provider=provider,
                    label=f"pool gate {account_id}",
                    status="active",
                    max_concurrent=slots,
                    secret_name="",
                    models=(model,),
                    created_at=created_at,
                )
            )
        self.scheduler = AccountScheduler(
            self.registry,
            # Match the plane's headroom: the gate fills the whole fleet, so
            # the global cap must not mask per-account slot exhaustion.
            max_global=accounts * slots + 4,
            clock=lambda: self.now[0],
        )
        self.app.state.account_registry = self.registry
        self.app.state.scheduler = self.scheduler
        self.app.state.api_key_store = self.keys

        _, self.agents_token = self.keys.create(label="gate-agents", scopes=("agents",))
        _, self.admin_token = self.keys.create(label="gate-admin", scopes=("agents", "admin"))
        self.secrets = [self.agents_token, self.admin_token]
        self.client = TestClient(self.app)
        self._stack = self.client.__enter__()

    # -- HTTP surface ---------------------------------------------------

    def _headers(self, admin: bool = False) -> dict[str, str]:
        token = self.admin_token if admin else self.agents_token
        return {"Authorization": f"Bearer {token}"}

    def me(self) -> dict[str, Any]:
        resp = self.client.get("/v1/me", headers=self._headers())
        self.captured.append(resp.text)
        return resp.json()

    def accounts(self, provider: str) -> list[dict[str, Any]] | None:
        resp = self.client.get(
            "/v1/accounts", params={"provider": provider}, headers=self._headers(admin=True)
        )
        self.captured.append(resp.text)
        if resp.status_code != 200:
            return None
        return list(resp.json().get("accounts") or [])

    def create(
        self, provider: str, account_id: str | None, prompt: str
    ) -> tuple[int, dict[str, Any]]:
        body: dict[str, Any] = {
            "prompt": {"text": prompt},
            "agent": {"provider": provider},
        }
        if account_id is not None:
            body["agent"]["account_id"] = account_id
        resp = self.client.post("/v1/agents", json=body, headers=self._headers())
        self.captured.append(resp.text)
        try:
            return resp.status_code, resp.json()
        except json.JSONDecodeError:
            return resp.status_code, {}

    def get_run(self, agent_id: str, run_id: str) -> tuple[int, dict[str, Any]]:
        resp = self.client.get(f"/v1/agents/{agent_id}/runs/{run_id}", headers=self._headers())
        self.captured.append(resp.text)
        try:
            return resp.status_code, resp.json()
        except json.JSONDecodeError:
            return resp.status_code, {}

    def delete(self, agent_id: str) -> int:
        resp = self.client.delete(f"/v1/agents/{agent_id}", headers=self._headers())
        self.captured.append(resp.text)
        return resp.status_code

    # -- control-plane internals (fake mode only) ------------------------

    def inject_failure(self, kind: str, account_id: str, retry_after: float | None) -> bool:
        try:
            self.scheduler.report_failure(account_id, kind, retry_after=retry_after)
        except KeyError:
            return False
        return True

    def advance_clock(self, seconds: float) -> None:
        self.now[0] = self.now[0] + timedelta(seconds=seconds)

    def kill_sandbox(self, agent_id: str) -> bool:
        rec = self.store.get(agent_id)
        handle = rec.handle() if rec is not None else None
        if handle is None:
            return False
        self.backend.terminate(handle)
        return True

    def reap(self) -> None:
        from control.reaper import reap
        from control.service import release_lease_for_action

        v1_state = getattr(self.app.state, "v1_state", None)
        reap(
            self.store,
            self.backend,
            self.now[0] + timedelta(seconds=10),
            on_action=lambda action: release_lease_for_action(v1_state, action),
        )

    def active_count(self) -> int | None:
        return self.scheduler.active_count

    def session_status(self, agent_id: str) -> str | None:
        rec = self.store.get(agent_id)
        return rec.status if rec is not None else None

    def settle(self, agent_id: str, timeout: float) -> str | None:
        """Wait for the async create's provisioner to leave ``creating``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.session_status(agent_id)
            if status is not None and status != "creating":
                return status
            time.sleep(0.05)
        return self.session_status(agent_id)

    def close(self) -> None:
        try:
            for handle in list(self.backend.list()):
                self.backend.terminate(handle)
        finally:
            self.client.__exit__(None, None, None)


class RealTransport:
    """HTTP client against a deployed control plane (``--real``)."""

    mode = "real"

    def __init__(self, base_url: str, token: str, *, timeout: float = 120.0) -> None:
        import httpx

        self.captured: list[str] = []
        self.secrets = [token]
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def me(self) -> dict[str, Any] | None:
        resp = self._client.get("/v1/me")
        self.captured.append(resp.text)
        if resp.status_code != 200:
            return None
        return resp.json()

    def accounts(self, provider: str) -> list[dict[str, Any]] | None:
        resp = self._client.get("/v1/accounts", params={"provider": provider})
        self.captured.append(resp.text)
        if resp.status_code != 200:
            return None
        return list(resp.json().get("accounts") or [])

    def create(
        self, provider: str, account_id: str | None, prompt: str
    ) -> tuple[int, dict[str, Any]]:
        body: dict[str, Any] = {
            "prompt": {"text": prompt},
            "agent": {"provider": provider},
        }
        if account_id is not None:
            body["agent"]["account_id"] = account_id
        resp = self._client.post("/v1/agents", json=body)
        self.captured.append(resp.text)
        try:
            return resp.status_code, resp.json()
        except json.JSONDecodeError:
            return resp.status_code, {}

    def get_run(self, agent_id: str, run_id: str) -> tuple[int, dict[str, Any]]:
        resp = self._client.get(f"/v1/agents/{agent_id}/runs/{run_id}")
        self.captured.append(resp.text)
        try:
            return resp.status_code, resp.json()
        except json.JSONDecodeError:
            return resp.status_code, {}

    def delete(self, agent_id: str) -> int:
        resp = self._client.delete(f"/v1/agents/{agent_id}")
        self.captured.append(resp.text)
        return resp.status_code

    # Control-plane internals are not reachable over the wire.
    def inject_failure(self, kind: str, account_id: str, retry_after: float | None) -> bool:
        return False

    def advance_clock(self, seconds: float) -> None:
        return None

    def kill_sandbox(self, agent_id: str) -> bool:
        return False

    def reap(self) -> None:
        return None

    def active_count(self) -> int | None:
        return None

    def session_status(self, agent_id: str) -> str | None:
        return None

    def settle(self, agent_id: str, timeout: float) -> str | None:
        return None

    def close(self) -> None:
        self._client.close()


# ---------------------------------------------------------------- gate


def _err_code(body: dict[str, Any]) -> str | None:
    error = body.get("error")
    return error.get("code") if isinstance(error, dict) else None


def _wait_run(transport: Any, agent_id: str, run_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _status, body = transport.get_run(agent_id, run_id)
        if body:
            last = body
            if body.get("status") in TERMINAL_RUN_STATUSES:
                return body
        time.sleep(1.0 if transport.mode == "real" else 0.1)
    return last


def run_gate(
    transport: Any,
    *,
    provider: str,
    expected: int,
    model: str,
    prompt: str,
    max_fanout: int,
    run_timeout: float,
) -> str:
    """Run the pool-gate matrix against ``transport``; return the verdict."""
    created: list[dict[str, Any]] = []
    verdict = "FAIL"
    try:
        # --- identity + fleet shape --------------------------------------
        me = transport.me()
        if not check("auth.me", isinstance(me, dict), f"me={me}"):
            return verdict
        if transport.mode == "real":
            scopes = set(me.get("scopes") or [])
            check(
                "auth.admin_scope",
                "admin" in scopes,
                "fleet introspection needs an admin-scoped key" if "admin" not in scopes else None,
            )

        fleet = transport.accounts(provider)
        ok = (
            isinstance(fleet, list)
            and len(fleet) >= expected
            and len({a["id"] for a in fleet}) == len(fleet)
            and all(a.get("provider") == provider for a in fleet)
        )
        check(
            "fleet.shape",
            ok,
            f"{len(fleet) if isinstance(fleet, list) else fleet} accounts, want >= {expected}",
        )
        if not fleet:
            return verdict
        record("fleet", sorted(a["id"] for a in fleet))
        fleet_ids = [a["id"] for a in fleet]

        # --- cooldown / failover -----------------------------------------
        if transport.mode == "fake":
            target = fleet_ids[0]
            check(
                "cooldown.inject",
                transport.inject_failure("rate_limited", target, 45.0),
            )
            status, body = transport.create(provider, "auto", prompt)
            landed = body.get("agent", {}).get("account_id")
            check("cooldown.auto_failover", status == 201 and landed != target, f"acct={landed}")
            if status == 201:
                transport.delete(body["agent"]["id"])
            status, body = transport.create(provider, target, prompt)
            check(
                "cooldown.named_refused",
                status == 409 and _err_code(body) == "account_unavailable",
                f"{status} {_err_code(body)}",
            )
            for account_id in fleet_ids:
                transport.inject_failure("rate_limited", account_id, 45.0)
            status, body = transport.create(provider, "auto", prompt)
            error = body.get("error") or {}
            check(
                "cooldown.exhausted_retry_after",
                status == 429
                and error.get("code") == "provider_exhausted"
                and isinstance(error.get("retry_after"), (int, float))
                and error["retry_after"] > 0,
                f"{status} {error}",
            )
            transport.advance_clock(90)
        else:
            degraded = [a for a in fleet if a.get("status") != "active"]
            if degraded:
                status, body = transport.create(provider, degraded[0]["id"], prompt)
                check(
                    "cooldown.named_refused",
                    status == 409 and _err_code(body) == "account_unavailable",
                    f"{status} {_err_code(body)}",
                )
            else:
                check(
                    "cooldown.named_refused",
                    True,
                    "fleet fully active; no degraded account to observe",
                )

        # --- auto rotation across the fleet --------------------------------
        fanout = min(expected, max_fanout)
        for _ in range(fanout):
            status, body = transport.create(provider, "auto", prompt)
            if status != 201:
                check("auto.rotation", False, f"create {status}: {_err_code(body)}")
                break
            created.append(body["agent"])
        else:
            picked = {a.get("account_id") for a in created}
            if transport.mode == "fake":
                check(
                    "auto.rotation",
                    len(picked) == expected,
                    f"{len(picked)}/{expected} accounts covered",
                )
            else:
                check(
                    "auto.rotation",
                    len(picked) >= min(2, expected),
                    f"{len(picked)} distinct accounts across {len(created)} creates",
                )
        record("rotation", [a.get("account_id") for a in created])

        # --- exhaustion ---------------------------------------------------
        capacity = sum(
            int(a.get("max_concurrent") or 0) for a in fleet if a.get("status") == "active"
        )
        if capacity <= max_fanout:
            refused_status, refused_body = None, None
            while len(created) < capacity:
                status, body = transport.create(provider, "auto", prompt)
                if status != 201:
                    break
                created.append(body["agent"])
            else:
                status, body = transport.create(provider, "auto", prompt)
            refused_status, refused_body = status, body
            check(
                "auto.exhausted_429",
                refused_status == 429 and _err_code(refused_body) == "provider_exhausted",
                f"{refused_status} {_err_code(refused_body)} after {len(created)} creates",
            )
        else:
            check(
                "auto.exhausted_429",
                True,
                f"capacity {capacity} > fanout cap {max_fanout}; exhaustion not exercised",
            )

        # --- named-account errors ------------------------------------------
        saturated = next(
            (
                a
                for a in created
                if sum(1 for c in created if c.get("account_id") == a.get("account_id"))
                >= next(
                    (f["max_concurrent"] for f in fleet if f["id"] == a.get("account_id")),
                    1,
                )
            ),
            None,
        )
        if saturated is not None:
            status, body = transport.create(provider, saturated["account_id"], prompt)
            check(
                "named.busy_409",
                status == 409 and _err_code(body) == "account_busy",
                f"{status} {_err_code(body)}",
            )
        else:
            check("named.busy_409", True, "no saturated account to pin")

        status, body = transport.create(provider, "acct-gate-missing", prompt)
        check(
            "named.unavailable_409",
            status == 409 and _err_code(body) == "account_unavailable",
            f"{status} {_err_code(body)}",
        )

        status, body = transport.create("bogus-provider-xyz", None, prompt)
        check(
            "invalid_provider.400",
            status == 400 and _err_code(body) == "invalid_provider",
            f"{status} {_err_code(body)}",
        )

        # --- runs reach terminal -------------------------------------------
        if transport.mode == "fake":
            for agent in created:
                transport.settle(agent["id"], timeout=30)
        statuses: dict[str, int] = {}
        runs_ok = True
        for agent in created:
            run = _wait_run(transport, agent["id"], "run-1", run_timeout)
            status = run.get("status")
            statuses[status or "none"] = statuses.get(status or "none", 0) + 1
            if status not in TERMINAL_RUN_STATUSES:
                runs_ok = False
                continue
            if status == "ERROR":
                error = run.get("error")
                canonical = (
                    isinstance(error, dict)
                    and isinstance(error.get("code"), str)
                    and isinstance(error.get("message"), str)
                    and isinstance(error.get("retryable"), bool)
                )
                runs_ok = runs_ok and canonical
        check("runs.terminal", runs_ok, f"statuses={statuses}")
        record("run_statuses", statuses)

        # --- reaper lease release (fake only) -------------------------------
        if transport.mode == "fake" and created:
            victim = created.pop(0)
            before = transport.active_count()
            check("reaper.kill", transport.kill_sandbox(victim["id"]))
            transport.reap()
            status = transport.session_status(victim["id"])
            after = transport.active_count()
            check(
                "reaper.releases_lease",
                status in ("timed_out", "lost") and after == before - 1,
                f"status={status} leases {before}->{after}",
            )

        # --- cleanup frees every slot ---------------------------------------
        for agent in created:
            transport.delete(agent["id"])
        created.clear()
        if transport.mode == "fake":
            check(
                "leases.drained",
                transport.active_count() == 0,
                f"active_count={transport.active_count()}",
            )
        refill = min(expected, max_fanout)
        refill_ok = True
        refill_agents: list[dict[str, Any]] = []
        for _ in range(refill):
            status, body = transport.create(provider, "auto", prompt)
            if status != 201:
                refill_ok = False
                break
            refill_agents.append(body["agent"])
        check(
            "leases.refill",
            refill_ok and len(refill_agents) == refill,
            f"{len(refill_agents)}/{refill} re-acquired after cleanup",
        )
        for agent in refill_agents:
            transport.delete(agent["id"])

        # --- credential leak scan -------------------------------------------
        leaks: list[str] = []
        for index, text in enumerate(transport.captured):
            reason = leak_reason(text, transport.secrets)
            if reason:
                leaks.append(f"response#{index}: {reason}")
        check(
            "leaks.bodies",
            not leaks,
            "; ".join(leaks) if leaks else f"{len(transport.captured)} responses scanned",
        )

        if CHECKS and all(c["ok"] for c in CHECKS):
            verdict = "PASS"
    except Exception:  # noqa: BLE001
        record("error", traceback.format_exc()[-2000:])
    finally:
        for agent in created:
            try:
                transport.delete(agent["id"])
            except Exception:  # noqa: BLE001
                pass
    return verdict


def _fake_transport(args: argparse.Namespace) -> FakeTransport:
    runner = Path(args.runner).expanduser()
    if not runner.is_file():
        raise FileNotFoundError(f"stub_runner not found: {runner}")
    return FakeTransport(
        args.provider,
        args.expect,
        model=args.model,
        runner=runner,
        slots=args.slots,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--provider", choices=sorted(SPECS), default="antigravity")
    ap.add_argument(
        "--expect",
        type=int,
        default=None,
        help="expected account count (default: per-provider spec)",
    )
    ap.add_argument("--model", default=None, help="account model (default: per-provider spec)")
    ap.add_argument("--slots", type=int, default=1, help="fake mode: per-account slot cap")
    ap.add_argument(
        "--real",
        action="store_true",
        help="drive a deployed control plane (or set SBX_POOL_GATE_REAL=1)",
    )
    ap.add_argument("--base-url", default=os.environ.get("SBX_V1_BASE_URL", ""))
    ap.add_argument(
        "--key-env",
        default="SBX_V1_API_KEY",
        help="env var holding the sbx_ bearer token (real mode)",
    )
    ap.add_argument(
        "--fanout",
        type=int,
        default=8,
        help="max concurrent agents created for rotation/exhaustion",
    )
    ap.add_argument(
        "--run-timeout", type=float, default=600.0, help="per-run terminal wait (seconds)"
    )
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument(
        "--runner",
        default=str(REPO_ROOT / "tests" / "fakes" / "stub_runner.py"),
        help="fake mode: stub runner path",
    )
    args = ap.parse_args()

    spec = SPECS[args.provider]
    args.expect = args.expect or spec.expected_accounts
    args.model = args.model or spec.model
    real = args.real or os.environ.get("SBX_POOL_GATE_REAL") == "1"

    transport: FakeTransport | RealTransport | None = None
    missing: list[str] = []
    if real:
        base_url = (args.base_url or "").strip()
        token = (os.environ.get(args.key_env) or "").strip()
        if not base_url:
            missing.append("no --base-url / SBX_V1_BASE_URL")
        if not token:
            missing.append(f"no bearer token in ${args.key_env}")
        if not missing:
            transport = RealTransport(base_url, token)
    else:
        try:
            transport = _fake_transport(args)
        except FileNotFoundError as exc:
            missing.append(str(exc))
    if missing:
        for msg in missing:
            print(f"[skip] {msg}", flush=True)
        write_json(
            f"pool_gate_{args.provider}.json",
            {"verdict": "SKIP", "missing": missing, "mode": "real" if real else "fake"},
        )
        return 2

    assert transport is not None
    reset_state()
    record(
        "params",
        {
            "mode": transport.mode,
            "provider": args.provider,
            "expected_accounts": args.expect,
            "model": args.model,
            "fanout": args.fanout,
        },
    )
    log(f"pool gate: mode={transport.mode} provider={args.provider} expect={args.expect}")
    try:
        verdict = run_gate(
            transport,
            provider=args.provider,
            expected=args.expect,
            model=args.model,
            prompt=args.prompt,
            max_fanout=args.fanout,
            run_timeout=args.run_timeout,
        )
    finally:
        transport.close()
    RESULTS["verdict"] = verdict
    RESULTS["failed_checks"] = [c["name"] for c in CHECKS if not c["ok"]]
    RESULTS["checks"] = CHECKS
    path = write_json(f"pool_gate_{args.provider}.json", RESULTS)
    log(f"verdict={verdict} failed={RESULTS['failed_checks']} artifact={path}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

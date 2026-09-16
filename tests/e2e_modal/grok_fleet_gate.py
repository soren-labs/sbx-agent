"""Release 0.1 gate: real Modal Grok fleet on the RC deployment.

Host-executed operator gate for the ``sbx-control-release01-rc`` control
plane. Validates the Grok 2-account pool end to end through the public
``/v1`` surface:

    fleet shape (grok-1/grok-2, one slot each)
    -> per-account credential verify + real run-1 auth
    -> account_id=auto LRU distribution, slot exhaustion, named-account errors
    -> two-turn resume (run-2 recalls a marker from run-1)
    -> control-plane restart: running counts survive, resume still works
    -> injected cooldown: named pick refused, auto fails over
    -> stale sandbox: out-of-band terminate -> normalized error
    -> cancel -> canonical run error shape
    -> cleanup -> zero leftover sandboxes -> credential leak scan

Prerequisites (checked up front; missing -> verdict SKIP, exit 2):

* ``SBX_V1_API_KEY`` — admin-scoped ``sbx_`` bearer for the RC endpoint.
* ``MODAL_PROFILE`` (default ``sorenlab2026``) with working Modal auth —
  used for the RC accounts Dict (cooldown injection / session lookup),
  the mid-gate ``modal deploy`` restart, and the leftover sandbox scan.
* The RC fleet already seeded: ``grok-1`` + ``grok-2`` at
  ``max_concurrent=1`` with ``sbx-rc-acct-grok-{1,2}`` Secrets present.

Fleet prep (recorded in the gate artifact): ``sbx-rc-acct-grok-2`` was
materialized from the production ``sbx-accounts`` ``credential/grok-2``
blob through the same credential-store path the RC deploy used
(``collect_credential_blob`` -> ``Secret.objects.create``), then the app
was redeployed with ``SBX_GROK_ACCOUNTS`` seeding both accounts at one
slot. No credential content is ever printed or written to the artifact —
only sha256-16 fingerprints.

Run (from repo root):

    SBX_V1_API_KEY=$(cat <rc-home>/.local/state/sbx/bootstrap.key) \
        uv run python -m tests.e2e_modal.grok_fleet_gate

Exit codes: 0 = PASS, 1 = FAIL, 2 = prerequisites missing (SKIP).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from control.backends.modal import ModalBackend  # noqa: E402
from control.reaper import reap  # noqa: E402
from control.store import ModalDictStore  # noqa: E402

from tests.e2e_modal.helpers import leak_reason, modal_app_logs, write_json  # noqa: E402

BASE_URL = "https://sorenlab2026--sbx-control-release01-rc-fastapi-app.modal.run"
APP_NAME = "sbx-control-release01-rc"
ACCOUNTS_DICT = "sbx-rc-accounts"
SESSIONS_DICT = "sbx-rc-sessions"
PROD_ACCOUNTS_DICT = "sbx-accounts"
SECRET_PREFIX = "sbx-rc-acct-"
ACCOUNTS = ("grok-1", "grok-2")
PROVIDER = "grok"
MODEL = "grok-4.6"
TERMINAL_RUN_STATUSES = ("FINISHED", "ERROR", "CANCELLED", "EXPIRED")
TERMINAL_SESSION_STATUSES = ("closed", "timed_out", "lost")
TRIVIAL_PROMPT = "Reply with exactly this line and nothing else: GROK_GATE_OK"

# Deploy-time env replayed on every (re)deploy: the isolated RC namespace
# from the RC bootstrap config plus the two-account Grok seed at one slot.
RC_DEPLOY_ENV = {
    "SBX_MODAL_APP_NAME": APP_NAME,
    "SBX_SESSIONS_DICT": "sbx-rc-sessions",
    "SBX_RUNS_DICT": "sbx-rc-runs",
    "SBX_ACCOUNTS_DICT": ACCOUNTS_DICT,
    "SBX_WORKFLOWS_DICT": "sbx-rc-workflows",
    "SBX_ARTIFACTS_DICT": "sbx-rc-artifacts",
    "SBX_WORKSPACES_DICT": "sbx-rc-workspaces",
    "SBX_ACCOUNT_SECRET_PREFIX": SECRET_PREFIX,
    "SBX_CODEX_SECRET_NAME": "sbx-rc-codex-auth",
    "SBX_BASIC_SECRET_NAME": "sbx-rc-basic-auth",
    "SBX_V1_BOOTSTRAP_SECRET_NAME": "sbx-rc-v1-bootstrap",
    "SBX_IMAGE_CODEX": "sbx-rc-runtime",
    "SBX_IMAGE_DEVIN": "sbx-rc-runtime-devin",
    "SBX_IMAGE_ANTIGRAVITY": "sbx-rc-runtime-antigravity",
    "SBX_IMAGE_GROK": "sbx-rc-runtime-grok",
    "SBX_IMAGE_OPENCODE": "sbx-rc-runtime-opencode",
    "SBX_GROK_ACCOUNTS": json.dumps(
        [
            {"id": "grok-1", "label": "P2 grok 1", "slots": 1},
            {"id": "grok-2", "label": "P2 grok 2", "slots": 1},
        ]
    ),
}

RESULTS: dict[str, Any] = {}
CHECKS: list[dict[str, Any]] = []
CAPTURED: list[str] = []
# Every key this gate mints (re-minted after a restart — the store is
# in-memory, so a redeploy wipes runtime keys). Sandbox ``owner`` tags keep
# the first key's id, so the leftover scan must know all of them.
GATE_KEY_IDS: set[str] = set()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def record(key: str, value: object) -> None:
    RESULTS[key] = value
    print(f"[result] {key} = {json.dumps(value, ensure_ascii=False, default=str)}", flush=True)


def check(name: str, ok: bool, detail: str | None = None) -> bool:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})
    mark = "ok" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


# ------------------------------------------------------------------ clients


class V1:
    """Thin httpx wrapper; every response body is captured for the leak scan."""

    def __init__(self, base_url: str, token: str, timeout: float = 180.0) -> None:
        import httpx

        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def call(self, method: str, path: str, **kw: Any) -> tuple[int, dict[str, Any]]:
        resp = self._client.request(method, path, **kw)
        CAPTURED.append(resp.text)
        try:
            return resp.status_code, resp.json()
        except json.JSONDecodeError:
            return resp.status_code, {"_non_json": True}

    def create_agent(self, account_id: str | None, prompt: str) -> tuple[int, dict[str, Any]]:
        agent: dict[str, Any] = {"provider": PROVIDER, "model": MODEL}
        if account_id is not None:
            agent["account_id"] = account_id
        return self.call("POST", "/v1/agents", json={"prompt": {"text": prompt}, "agent": agent})

    def close(self) -> None:
        self._client.close()


def _err(body: dict[str, Any]) -> dict[str, Any] | None:
    error = body.get("error")
    return error if isinstance(error, dict) else None


def _err_code(body: dict[str, Any]) -> str | None:
    error = _err(body)
    return error.get("code") if error else None


def wait_run(v1: V1, agent_id: str, run_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _status, body = v1.call("GET", f"/v1/agents/{agent_id}/runs/{run_id}")
        if body:
            last = body
            if body.get("status") in TERMINAL_RUN_STATUSES:
                return body
        time.sleep(2.0)
    return last


def accounts(v1: V1) -> list[dict[str, Any]]:
    status, body = v1.call("GET", "/v1/accounts", params={"provider": PROVIDER})
    if status != 200:
        return []
    return list(body.get("accounts") or [])


def running_map(v1: V1) -> dict[str, int | None]:
    return {a["id"]: a.get("running") for a in accounts(v1)}


def delete_agent(v1: V1, agent_id: str) -> int:
    status, _body = v1.call("DELETE", f"/v1/agents/{agent_id}")
    return status


# --------------------------------------------------------------- modal seams


def _modal() -> Any:
    import modal

    return modal


class _ScopedStore:
    """SessionStore view limited to one session id: the real RC Dict stays
    the source of truth, but a gate-driven reaper sweep must never see —
    or touch — sibling gates' records."""

    def __init__(self, inner: Any, session_id: str) -> None:
        self._inner = inner
        self._sid = session_id

    def get(self, session_id: str) -> Any:
        return self._inner.get(session_id)

    def put(self, record: Any) -> None:
        self._inner.put(record)

    def list_all(self) -> list[Any]:
        rec = self._inner.get(self._sid)
        return [rec] if rec is not None else []

    def delete(self, session_id: str) -> None:
        self._inner.delete(session_id)


class _ScopedBackend:
    """SandboxBackend view that delegates poll/terminate to the real Modal
    backend but lists nothing — the reaper's orphan pass must not see
    sibling gates' sandboxes."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def poll(self, handle: Any) -> Any:
        return self._inner.poll(handle)

    def terminate(self, handle: Any) -> None:
        self._inner.terminate(handle)

    def list(self, tags: Any = None) -> list[Any]:
        return []


def accounts_dict() -> Any:
    return _modal().Dict.from_name(ACCOUNTS_DICT, create_if_missing=False)


def set_account_status(
    account_id: str, status: str, *, cooldown_until: str | None = None, last_error: Any = None
) -> None:
    """Operator write into the RC accounts Dict (live-read by the scheduler)."""
    d = accounts_dict()
    rec = d.get(f"account/{account_id}")
    if not isinstance(rec, dict):
        raise KeyError(f"no account/{account_id} in {ACCOUNTS_DICT}")
    rec = dict(rec)
    rec["status"] = status
    rec["cooldown_until"] = cooldown_until
    rec["last_error"] = last_error
    d.put(f"account/{account_id}", rec)


def session_sandbox_id(agent_id: str) -> str | None:
    raw = _modal().Dict.from_name(SESSIONS_DICT, create_if_missing=False).get(agent_id)
    if isinstance(raw, dict):
        return raw.get("sandbox_id")
    return None


def rc_sandboxes() -> list[Any]:
    app = _modal().App.lookup(APP_NAME)
    return list(_modal().Sandbox.list(app_id=app.app_id))


def gate_sandboxes() -> list[str]:
    """Live RC sandboxes owned by any gate-minted key."""
    app = _modal().App.lookup(APP_NAME)
    out: list[str] = []
    for key_id in GATE_KEY_IDS:
        for s in _modal().Sandbox.list(app_id=app.app_id, tags={"owner": key_id}):
            out.append(s.object_id)
    return out


def mint_key(bootstrap: V1) -> tuple[str | None, str | None]:
    """Mint a dedicated agents+admin key; returns (key_id, token)."""
    status, body = bootstrap.call(
        "POST",
        "/v1/api-keys",
        json={"label": "p21-gate-grok-fleet", "scopes": ["agents", "admin"]},
    )
    # The create response echoes the raw key — keep it out of the scan.
    if CAPTURED:
        CAPTURED.pop()
    if status != 201 or not body.get("key"):
        return None, None
    GATE_KEY_IDS.add(str(body["id"]))
    return str(body["id"]), str(body["key"])


class GateClient:
    """Dedicated-key client that survives control-plane restarts.

    The v1 key store is in-memory: any redeploy (this gate's restart check
    or a sibling gate's) wipes runtime keys. A 401 therefore means "re-mint
    and retry once" rather than a hard failure.
    """

    def __init__(self, base_url: str, bootstrap: V1) -> None:
        self._base_url = base_url
        self._bootstrap = bootstrap
        self._v1: V1 | None = None
        self.tokens: list[str] = []

    def ensure(self) -> bool:
        if self._v1 is None:
            _kid, token = mint_key(self._bootstrap)
            if token is None:
                return False
            self.tokens.append(token)
            self._v1 = V1(self._base_url, token)
        return True

    def call(self, method: str, path: str, **kw: Any) -> tuple[int, dict[str, Any]]:
        if not self.ensure():
            return 0, {"_error": "key mint failed"}
        status, body = self._v1.call(method, path, **kw)
        if status == 401:
            self._v1.close()
            self._v1 = None
            if not self.ensure():
                return status, body
            status, body = self._v1.call(method, path, **kw)
        return status, body

    def peek(self, method: str, path: str, **kw: Any) -> int:
        """One-shot call without the 401 re-mint (for restart detection)."""
        if self._v1 is None:
            return 0
        status, _body = self._v1.call(method, path, **kw)
        return status

    def create_agent(self, account_id: str | None, prompt: str) -> tuple[int, dict[str, Any]]:
        agent: dict[str, Any] = {"provider": PROVIDER, "model": MODEL}
        if account_id is not None:
            agent["account_id"] = account_id
        return self.call("POST", "/v1/agents", json={"prompt": {"text": prompt}, "agent": agent})

    def close(self) -> None:
        if self._v1 is not None:
            self._v1.close()
            self._v1 = None


def ensure_fleet() -> None:
    """Operator-level fleet fix: upsert grok-1/grok-2 at one slot each.

    Equivalent to the ``SBX_GROK_ACCOUNTS`` deploy seed, but written
    directly into the durable accounts Dict — sibling gates redeploy this
    shared RC app with their own env, and the default grok seed would reset
    ``grok-1`` to 4 slots. Runtime fields (status/cooldown/lru) reset too:
    this gate defines the fleet's expected state outright.
    """
    d = accounts_dict()
    now = datetime.now(UTC).isoformat()
    for account_id in ACCOUNTS:
        existing = d.get(f"account/{account_id}")
        existing = existing if isinstance(existing, dict) else {}
        d.put(
            f"account/{account_id}",
            {
                "id": account_id,
                "provider": PROVIDER,
                "label": str(existing.get("label") or f"P2 {account_id}"),
                "status": "active",
                "max_concurrent": 1,
                "secret_name": f"{SECRET_PREFIX}{account_id}",
                "models": [MODEL],
                "created_at": str(existing.get("created_at") or now),
                "last_used_at": existing.get("last_used_at"),
                "cooldown_until": None,
                "last_error": None,
            },
        )


def redeploy() -> tuple[int, str]:
    """Cycle the RC app (same overlay every time — the grok seed included)."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "MODAL_PROFILE": os.environ.get("MODAL_PROFILE") or "sorenlab2026",
        **RC_DEPLOY_ENV,
    }
    proc = subprocess.run(
        [sys.executable, "-m", "modal", "deploy", "-m", "control.modal_app"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def wait_me(v1: V1, timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _body = v1.call("GET", "/v1/me")
            if status == 200:
                return True
        except Exception:  # noqa: BLE001 - cold start resets connections
            pass
        time.sleep(3.0)
    return False


# ---------------------------------------------------------------- leak scan


def credential_watch_values() -> list[str]:
    """Secret substrings that must never appear in captured output.

    Reads the stored prod blobs to build the watch list — contents are only
    ever compared against, never printed.
    """
    secrets: list[str] = []
    try:
        d = _modal().Dict.from_name(PROD_ACCOUNTS_DICT, create_if_missing=False)
        for account_id in ACCOUNTS:
            blob = d.get(f"credential/{account_id}")
            for content in (blob or {}).get("files", {}).values():
                if isinstance(content, str) and len(content) >= 8:
                    secrets.append(content)
                    try:
                        doc = json.loads(content)
                    except json.JSONDecodeError:
                        continue
                    for value in _secret_strings(doc):
                        secrets.append(value)
    except Exception as exc:  # noqa: BLE001
        log(f"watch-values unavailable: {exc.__class__.__name__}")
    return secrets


_SECRET_VALUE_KEYS = {
    "key",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "secret",
    "signature",
}


def _secret_strings(node: Any, depth: int = 0) -> list[str]:
    out: list[str] = []
    if depth > 6:
        return out
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                isinstance(value, str)
                and str(key).lower() in _SECRET_VALUE_KEYS
                and len(value) >= 8
            ):
                out.append(value)
            else:
                out.extend(_secret_strings(value, depth + 1))
    elif isinstance(node, list):
        for item in node:
            out.extend(_secret_strings(item, depth + 1))
    return out


def scan_leaks(label: str, text: str, secrets: list[str]) -> bool:
    if not text:
        return check(f"leak:{label}", True)
    for i, secret in enumerate(secrets):
        if secret and secret in text:
            return check(f"leak:{label}", False, f"credential value #{i} present")
    why = leak_reason(text)
    if why:
        return check(f"leak:{label}", False, why)
    return check(f"leak:{label}", True)


# --------------------------------------------------------------------- gate


def run_gate(bootstrap: V1, args: argparse.Namespace, secrets: list[str]) -> str:
    """Drive the gate with a dedicated API key.

    The per-key ``concurrency_limit`` cap counts every live sandbox owned by
    a key — other gates share the RC deployment under the same bootstrap
    key, so gate traffic must run under its own key to be deterministic.
    """
    verdict = "FAIL"
    created: list[dict[str, Any]] = []
    deferred: list[str] = []
    marker = f"GATE-{uuid.uuid4().hex[:8].upper()}"
    record("marker", marker)
    record("base_url", BASE_URL)
    record("head", os.environ.get("GATE_HEAD") or "unknown")

    def agent_of(account: str) -> dict[str, Any] | None:
        return next((a for a in created if a.get("account_id") == account), None)

    all_ids: list[str] = []

    def create_tracked(account_id: str | None, prompt: str) -> tuple[int, dict[str, Any]]:
        """Create an agent; every 201 lands in ``created`` for cleanup."""
        status, body = v1.create_agent(account_id, prompt)
        if status == 201 and isinstance(body.get("agent"), dict):
            created.append(body["agent"])
            all_ids.append(str(body["agent"]["id"]))
        return status, body

    v1 = GateClient(args.base_url, bootstrap)
    try:
        sandboxes_before = {s.object_id for s in rc_sandboxes()}
        record("sandboxes.before", sorted(sandboxes_before))

        # --- identity + dedicated gate key + fleet upsert ------------------
        status, me = bootstrap.call("GET", "/v1/me")
        check(
            "auth.me",
            status == 200 and "admin" in (me.get("scopes") or []),
            f"{status}",
        )
        check("auth.gate_key", v1.ensure(), "mint dedicated key")
        record("gate.key_ids", sorted(GATE_KEY_IDS))

        ensure_fleet()
        # The accounts read can hit a deploy cutover/cold-start window under
        # sibling gates; retry until both grok accounts are listed.
        deadline = time.monotonic() + 90
        fleet: dict[str, dict[str, Any]] = {}
        while time.monotonic() < deadline:
            fleet = {a["id"]: a for a in accounts(v1)}
            if all(a in fleet for a in ACCOUNTS):
                break
            time.sleep(3)
        ok = (
            set(fleet) == set(ACCOUNTS)
            and all(fleet[a].get("status") == "active" for a in ACCOUNTS)
            and all(fleet[a].get("max_concurrent") == 1 for a in ACCOUNTS)
        )
        shape = {
            a: {
                "status": fleet.get(a, {}).get("status"),
                "slots": fleet.get(a, {}).get("max_concurrent"),
            }
            for a in fleet
        }
        check("fleet.shape", ok, json.dumps(shape))
        if not ok:
            return verdict

        # --- per-account credential verify (runner init, real Secret) -----
        for account_id in ACCOUNTS:
            status, body = v1.call("POST", f"/v1/accounts/{account_id}/verify")
            state = body.get("status")
            check(
                f"verify.{account_id}",
                status == 200 and state == "active",
                f"http={status} status={state} last_error={body.get('last_error')}",
            )
            if state == "invalid" or body.get("last_error") == "auth_invalid":
                deferred.append(account_id)
        record("credential_deferred", deferred)
        if deferred:
            log("CREDENTIAL_DEFERRED: " + ", ".join(deferred))
            return verdict

        # --- auto distribution: one slot each -----------------------------
        # Sibling gates share this deployment and occasionally take grok
        # slots under the bootstrap key. Wait for foreign sessions to drain
        # so the pool starts empty, then re-pin the fleet (a sibling
        # redeploy re-seeds grok-1 to the 4-slot default).
        deadline = time.monotonic() + 240
        while True:
            counts = running_map(v1)
            if not any(counts.get(a) for a in ACCOUNTS):
                break
            if time.monotonic() > deadline:
                check("fleet.foreign_drained", False, f"running={counts}")
                return verdict
            time.sleep(5)
        check("fleet.foreign_drained", True)
        ensure_fleet()
        # Sibling gates share this fleet: a transient 429 just means another
        # session grabbed a slot in the race window, so retry until each
        # account has been picked once (or the deadline passes).
        deadline = time.monotonic() + 360
        picked: list[str] = []
        while len(picked) < len(ACCOUNTS):
            if time.monotonic() > deadline:
                break
            status, body = create_tracked("auto", TRIVIAL_PROMPT)
            if status == 201:
                acct = str(created[-1].get("account_id"))
                if acct in picked:
                    # a sibling reseeded the account wider; re-pin and release
                    delete_agent(v1, created.pop()["id"])
                    ensure_fleet()
                    continue
                picked.append(acct)
                continue
            if status == 429 and _err_code(body) in (
                "provider_exhausted",
                "concurrency_limit",
            ):
                ensure_fleet()
                time.sleep(10)
                continue
            check("auto.create", False, f"{status} {_err_code(body)}")
            break
        check(
            "auto.distribution",
            len(created) == len(ACCOUNTS) and sorted(picked) == sorted(ACCOUNTS),
            f"picked={picked}",
        )
        record("auto.picked", picked)

        ensure_fleet()
        status, body = create_tracked("auto", TRIVIAL_PROMPT)
        check(
            "auto.exhausted_429",
            status == 429 and _err_code(body) == "provider_exhausted",
            f"{status} {_err(body)}",
        )
        busy = created[0]["account_id"] if created else ACCOUNTS[0]
        status, body = create_tracked(busy, TRIVIAL_PROMPT)
        check(
            "named.busy_409",
            status == 409 and _err_code(body) == "account_busy",
            f"{status} {_err_code(body)}",
        )
        status, body = create_tracked("grok-missing", TRIVIAL_PROMPT)
        check(
            "named.unavailable_409",
            status == 409 and _err_code(body) == "account_unavailable",
            f"{status} {_err_code(body)}",
        )
        counts = running_map(v1)
        check(
            "slots.running_counts",
            counts.get(ACCOUNTS[0]) == 1 and counts.get(ACCOUNTS[1]) == 1,
            f"running={counts}",
        )

        # --- run-1 on both: turn-level auth proof for each account --------
        run1: dict[str, dict[str, Any]] = {}
        for agent in created:
            run = wait_run(v1, agent["id"], "run-1", args.run_timeout)
            run1[agent["id"]] = run
            error = run.get("error") or {}
            ok = run.get("status") == "FINISHED"
            check(
                f"run1.finished.{agent.get('account_id')}",
                ok,
                f"status={run.get('status')} code={error.get('code')}",
            )
            if error.get("code") == "auth_invalid":
                deferred.append(str(agent.get("account_id")))
        record("credential_deferred", deferred)
        if deferred:
            log("CREDENTIAL_DEFERRED: " + ", ".join(deferred))
        a1 = created[0] if created else None
        if a1 is None or not all(r.get("status") == "FINISHED" for r in run1.values()):
            return verdict

        # --- two-turn resume on A1 ----------------------------------------
        recall1 = (
            f"Remember this exact phrase for the rest of our conversation: {marker}\n"
            "Then reply with exactly this line and nothing else: GATE_TURN1_DONE"
        )
        # run-1 already ran a trivial prompt; run-2 stores the marker.
        status, body = v1.call(
            "POST",
            f"/v1/agents/{a1['id']}/runs",
            json={"prompt": {"text": recall1}},
        )
        check("run2.dispatched", status == 201, f"{status} {_err_code(body)}")
        run2 = wait_run(v1, a1["id"], "run-2", args.run_timeout)
        check("run2.finished", run2.get("status") == "FINISHED", f"{run2.get('status')}")

        recall2 = (
            "Without reading any files or running any commands, what exact phrase "
            "did I ask you to remember earlier in this conversation? "
            "Reply with exactly that phrase and nothing else."
        )
        status, body = v1.call(
            "POST",
            f"/v1/agents/{a1['id']}/runs",
            json={"prompt": {"text": recall2}},
        )
        check("run3.dispatched", status == 201, f"{status} {_err_code(body)}")
        run3 = wait_run(v1, a1["id"], "run-3", args.run_timeout)
        text3 = ((run3.get("result") or {}).get("text")) or ""
        check(
            "run3.resume_recall",
            run3.get("status") == "FINISHED" and marker in text3,
            f"status={run3.get('status')} marker={'present' if marker in text3 else 'absent'}",
        )
        record(
            "two_turn",
            {
                "run2_status": run2.get("status"),
                "run3_status": run3.get("status"),
                "recalled": marker in text3,
            },
        )
        if not all(c["ok"] for c in CHECKS):
            return verdict

        # --- control-plane restart: running counts + resume survive -------
        log("restarting RC app (modal deploy, identical overlay)")
        rc, out = redeploy()
        check("restart.deploy_rc0", rc == 0, out.strip()[-200:] if rc else None)
        check("restart.me_200", wait_me(bootstrap), None)
        # The runtime key store is in-memory: the new container re-seeds
        # only the bootstrap key, so the gate key dies on cutover. A 401 on
        # the old key proves the new store is actually serving.
        deadline = time.monotonic() + 120
        new_store = False
        while time.monotonic() < deadline:
            if v1.peek("GET", "/v1/me") == 401:
                new_store = True
                break
            time.sleep(2)
        check(
            "restart.key_store_cycled",
            new_store,
            "old gate key still valid" if not new_store else None,
        )
        # GateClient re-mints transparently on the next call.
        status, _me2 = v1.call("GET", "/v1/me")
        check("restart.new_key_works", status == 200, f"{status}")
        record("gate.key_ids", sorted(GATE_KEY_IDS))
        # The accounts read can hit a cold-start/transient window right after
        # cutover; wait until both grok accounts are listed, then compare.
        deadline = time.monotonic() + 90
        counts: dict[str, int | None] = {}
        while time.monotonic() < deadline:
            counts = running_map(v1)
            if all(a in counts for a in ACCOUNTS):
                break
            time.sleep(3)
        check(
            "restart.running_counts",
            counts.get(ACCOUNTS[0]) == 1 and counts.get(ACCOUNTS[1]) == 1,
            f"running={counts} after restart",
        )
        ensure_fleet()
        status, body = create_tracked("auto", TRIVIAL_PROMPT)
        check(
            "restart.still_exhausted",
            status == 429 and _err_code(body) == "provider_exhausted",
            f"{status} {_err_code(body)}",
        )
        # Follow-up on the pre-restart agent: stale-to-new-container resume.
        status, body = v1.call(
            "POST",
            f"/v1/agents/{a1['id']}/runs",
            json={"prompt": {"text": recall2}},
        )
        check("restart.rerun_dispatched", status == 201, f"{status} {_err_code(body)}")
        run4 = wait_run(v1, a1["id"], "run-4", args.run_timeout)
        text4 = ((run4.get("result") or {}).get("text")) or ""
        check(
            "restart.resume_recall",
            run4.get("status") == "FINISHED" and marker in text4,
            f"status={run4.get('status')} marker={'present' if marker in text4 else 'absent'}",
        )

        # --- cooldown / failover -------------------------------------------
        cooled = next(
            (a.get("account_id") for a in created if a.get("account_id") != a1.get("account_id")),
            ACCOUNTS[1],
        )
        healthy = a1.get("account_id") or ACCOUNTS[0]
        until = (datetime.now(UTC) + timedelta(minutes=15)).isoformat()
        set_account_status(cooled, "cooling", cooldown_until=until, last_error="rate_limited")
        status, body = create_tracked(cooled, TRIVIAL_PROMPT)
        check(
            "cooldown.named_refused",
            status == 409 and _err_code(body) == "account_unavailable",
            f"{status} {_err(body)}",
        )
        # Free the healthy account's slot; auto must fail over past `cooled`.
        delete_agent(v1, a1["id"])
        created.remove(a1)
        status, body = create_tracked("auto", TRIVIAL_PROMPT)
        landed = (body.get("agent") or {}).get("account_id")
        check(
            "cooldown.auto_failover",
            status == 201 and landed == healthy,
            f"{status} acct={landed}",
        )
        a3 = body.get("agent") if status == 201 else None
        if a3:
            run_a3 = wait_run(v1, a3["id"], "run-1", args.run_timeout)
            check(
                "cooldown.failover_finished",
                run_a3.get("status") == "FINISHED",
                f"status={run_a3.get('status')}",
            )
        set_account_status(cooled, "active", cooldown_until=None, last_error=None)
        restored = accounts_dict().get(f"account/{cooled}") or {}
        check(
            "cooldown.restored",
            restored.get("status") == "active" and restored.get("cooldown_until") is None,
            f"status={restored.get('status')}",
        )

        # --- stale sandbox: out-of-band terminate -> normalized error -----
        # Re-assert this branch's build before the normalization check:
        # sibling gates share the RC app and their deploys can revert the
        # serving code between this gate's own restart and this section.
        log("re-asserting gate build before stale checks (shared RC app)")
        rc, _out = redeploy()
        if rc == 0:
            wait_me(bootstrap)
        a2 = agent_of(cooled)
        if a2 is not None:
            sb_id = session_sandbox_id(a2["id"])
            record("stale.sandbox_id_present", bool(sb_id))
            if sb_id:
                try:
                    _modal().Sandbox.from_id(sb_id).terminate()
                except Exception as exc:  # noqa: BLE001
                    log(f"sandbox terminate: {exc.__class__.__name__}")
                status, body = v1.call(
                    "POST",
                    f"/v1/agents/{a2['id']}/runs",
                    json={"prompt": {"text": TRIVIAL_PROMPT}},
                )
                check(
                    "stale.followup_normalized",
                    status == 409 and _err_code(body) == "session_not_runnable",
                    f"{status} {_err_code(body)}",
                )
                _st, agent_body = v1.call("GET", f"/v1/agents/{a2['id']}")
                check(
                    "stale.session_terminal",
                    agent_body.get("status") in ("lost", "closed", "timed_out"),
                    f"status={agent_body.get('status')}",
                )
            delete_agent(v1, a2["id"])
            created.remove(a2)

        # --- cancel: canonical run error ----------------------------------
        status, body = create_tracked("auto", TRIVIAL_PROMPT)
        a4 = body.get("agent") if status == 201 else None
        if a4:
            _st, cancel_body = v1.call("POST", f"/v1/agents/{a4['id']}/runs/run-1/cancel")
            run_c = wait_run(v1, a4["id"], "run-1", args.run_timeout)
            error = run_c.get("error") or {}
            canonical = (
                run_c.get("status") == "CANCELLED"
                and isinstance(error.get("code"), str)
                and isinstance(error.get("message"), str)
                and isinstance(error.get("retryable"), bool)
            )
            check(
                "cancel.canonical_error",
                canonical,
                f"status={run_c.get('status')} error={error}",
            )
            delete_agent(v1, a4["id"])
            created.remove(a4)

        # --- runs on gone agents: closed -> 409, unknown id -> 404 ---------
        if a4:
            status, body = v1.call(
                "POST",
                f"/v1/agents/{a4['id']}/runs",
                json={"prompt": {"text": TRIVIAL_PROMPT}},
            )
            check(
                "closed.run_409",
                status == 409 and _err_code(body) == "session_not_runnable",
                f"{status} {_err_code(body)}",
            )
        status, body = v1.call(
            "POST",
            "/v1/agents/agt-does-not-exist/runs",
            json={"prompt": {"text": TRIVIAL_PROMPT}},
        )
        check(
            "unknown.run_404",
            status == 404 and _err_code(body) == "not_found",
            f"{status} {_err_code(body)}",
        )

        # --- stranded running: watcher-loss slot leak must be reaped -------
        # Reproduces the observed cutover failure: a session record stuck
        # ``running`` on a live sandbox (its turn watcher died with the old
        # container) held the account slot forever. The record is fabricated
        # stale past the runner's own --max-seconds bound + run_grace, then
        # the real ``reap()`` runs scoped to this session — sibling gates
        # share the app, so the sweep must never see their sandboxes.
        a5 = None
        a5_status = 0
        a5_body: dict[str, Any] = {}
        for _attempt in range(6):
            a5_status, a5_body = create_tracked("auto", TRIVIAL_PROMPT)
            if a5_status == 201:
                a5 = a5_body["agent"]
                break
            if a5_status == 429:
                time.sleep(10)
                continue
            break
        if a5 is None:
            check(
                "reaper.stranded_running",
                False,
                f"create {a5_status} {_err_code(a5_body)}",
            )
        else:
            run_a5 = wait_run(v1, a5["id"], "run-1", args.run_timeout)
            if run_a5.get("status") != "FINISHED":
                check(
                    "reaper.stranded_running",
                    False,
                    f"run-1 status={run_a5.get('status')}",
                )
            else:
                sb_id = session_sandbox_id(a5["id"])
                try:
                    sd = _modal().Dict.from_name(SESSIONS_DICT, create_if_missing=False)
                    raw = sd.get(a5["id"])
                except Exception:  # noqa: BLE001
                    raw = None
                if not isinstance(raw, dict) or not sb_id:
                    check(
                        "reaper.stranded_running",
                        False,
                        "session record/sandbox unavailable",
                    )
                else:
                    stale_ts = (datetime.now(UTC) - timedelta(seconds=1500)).isoformat()
                    raw = dict(raw)
                    raw["status"] = "running"
                    raw["current_turn_id"] = "turn-9"
                    raw["current_turn_n"] = 9
                    raw["updated_at"] = stale_ts
                    raw["last_activity_at"] = stale_ts
                    sd.put(a5["id"], raw)
                    store = _ScopedStore(ModalDictStore(SESSIONS_DICT), a5["id"])
                    backend = _ScopedBackend(ModalBackend(APP_NAME))
                    actions = reap(store, backend, datetime.now(UTC))
                    _st, agent_body = v1.call("GET", f"/v1/agents/{a5['id']}")
                    check(
                        "reaper.stranded_running",
                        agent_body.get("status") in TERMINAL_SESSION_STATUSES
                        and any(a.kind == "lost" for a in actions),
                        f"status={agent_body.get('status')} actions={[a.kind for a in actions]}",
                    )
            delete_agent(v1, a5["id"])
            created.remove(a5)

        # --- cleanup: every gate-created agent reaches terminal ------------
        for agent in list(created):
            delete_agent(v1, agent["id"])
        created.clear()
        # Account-level running counts are not attributable while sibling
        # gates hold sessions on this fleet; verify each agent this gate
        # created is terminal instead.
        deadline = time.monotonic() + 60
        pending = set(all_ids)
        while pending and time.monotonic() < deadline:
            for aid in list(pending):
                _st, body = v1.call("GET", f"/v1/agents/{aid}")
                if body.get("status") in TERMINAL_SESSION_STATUSES:
                    pending.discard(aid)
            if pending:
                time.sleep(2)
        check(
            "cleanup.agents_terminal",
            not pending,
            f"pending={sorted(pending)}",
        )
        record("cleanup.running_counts", running_map(v1))
        time.sleep(10)  # let Modal teardown settle before the leftover scan
        # Scoped to sandboxes owned by the gate's keys: other gates share
        # this RC deployment and their sandboxes are not ours to judge.
        left = gate_sandboxes()
        check("cleanup.zero_leftovers", not left, f"left={left}")
        record("leftovers", left)

        # --- credential leak scan -------------------------------------------
        secrets.extend(v1.tokens)
        for index, text in enumerate(CAPTURED):
            scan_leaks(f"response#{index}", text, secrets)
        scan_leaks("app_logs", modal_app_logs(APP_NAME), secrets)

        if CHECKS and all(c["ok"] for c in CHECKS):
            verdict = "PASS"
    except Exception:  # noqa: BLE001
        record("error", traceback.format_exc()[-2000:])
    finally:
        for agent in created:
            try:
                delete_agent(v1, agent["id"])
            except Exception:  # noqa: BLE001
                pass
        # Best-effort restore: never leave a gate-injected cooldown behind.
        try:
            for account_id in ACCOUNTS:
                rec = accounts_dict().get(f"account/{account_id}")
                if isinstance(rec, dict) and rec.get("status") == "cooling":
                    set_account_status(account_id, "active", cooldown_until=None)
        except Exception:  # noqa: BLE001
            pass
        v1.close()
        for key_id in GATE_KEY_IDS:
            try:
                bootstrap.call("DELETE", f"/v1/api-keys/{key_id}")
            except Exception:  # noqa: BLE001
                pass
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--base-url", default=os.environ.get("SBX_V1_BASE_URL") or BASE_URL)
    ap.add_argument("--key-env", default="SBX_V1_API_KEY")
    ap.add_argument("--run-timeout", type=float, default=420.0)
    args = ap.parse_args()

    missing: list[str] = []
    token = (os.environ.get(args.key_env) or "").strip()
    if not token:
        missing.append(f"no bearer token in ${args.key_env}")
    try:
        _modal()
    except ImportError:
        missing.append("modal package unavailable")
    if missing:
        for msg in missing:
            print(f"[skip] {msg}", flush=True)
        write_json(
            "grok_fleet_gate.json",
            {"verdict": "SKIP", "missing": missing},
        )
        return 2

    record("params", {"app": APP_NAME, "accounts": list(ACCOUNTS), "model": MODEL})
    secrets = [token, *credential_watch_values()]
    log(f"grok fleet gate: base={args.base_url} accounts={ACCOUNTS}")
    v1 = V1(args.base_url, token)
    try:
        verdict = run_gate(v1, args, secrets)
    finally:
        v1.close()
    RESULTS["verdict"] = verdict
    RESULTS["credential_deferred"] = RESULTS.get("credential_deferred", [])
    RESULTS["failed_checks"] = [c["name"] for c in CHECKS if not c["ok"]]
    RESULTS["checks"] = CHECKS
    path = write_json("grok_fleet_gate.json", RESULTS)
    log(f"verdict={verdict} failed={RESULTS['failed_checks']} artifact={path}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

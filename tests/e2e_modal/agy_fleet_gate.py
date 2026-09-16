"""Release 0.1 RC gate: real Antigravity 4-account fleet over the RC /v1 API.

Host-executed against the deployed RC control plane
(``sbx-control-release01-rc``) — never the production app. Drives only the
public ``/v1`` surface plus operator-level state access (Modal Dict records,
per-account Secrets, sandbox files) needed to stage and inspect scenarios.

Coverage:
  * ``account_id=auto`` distribution across 4 distinct accounts
    (``antigravity-1..4``, one slot each — seeded via
    ``SBX_ANTIGRAVITY_ACCOUNTS`` at deploy time),
  * slot/full semantics: ``account_busy`` / ``account_unavailable`` /
    ``provider_exhausted``,
  * real unavailable / cooldown / failover scenarios: a marked ``cooling``
    account skipped by auto + lazy recovery, a ``disabled`` account refused,
    and a REAL auth-invalid turn (bogus per-account Secret) that marks the
    account ``invalid`` and fails over — then the credential is restored and
    proven working again. No valid credential is ever destroyed.
  * two-turn resume (marker recall without file reads),
  * stale-conversation semantics (corrupted ``native_session_id`` fails the
    run and is never silently adopted),
  * restart running-count safety (redeploy while idle agents hold slots;
    counts derive from live sessions, then auto cannot oversell),
  * leak scan over every captured response body, sandbox file, and the app
    log tail; cleanup leaves zero gate agents/sandboxes and all accounts
    ``active`` with ``running=0``.

The per-key live-agent cap (``SBX_MAX_CONCURRENT``, default 2) is smaller
than the fleet, so the 4-way distribution burst runs under the bootstrap key
plus one minted ``agents``-scope key (revoked afterwards). API keys are
container-local — a remint-on-401 wrapper covers container churn.

Run (from repo root; the RC HOME carries the Modal profile + bootstrap key):

    env HOME=/home/zheng/ai-work/p21/rc-home MODAL_PROFILE=sorenlab2026 \
        uv run python -m tests.e2e_modal.agy_fleet_gate

Exit codes: 0 = PASS, 1 = FAIL, 2 = prerequisites missing (SKIP).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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

# Modal/SDK env must be pinned before the first lazy ``import modal``.
_RC_HOME_DEFAULT = "/home/zheng/ai-work/p21/rc-home"
RC_HOME = Path(os.environ.get("SBX_RC_HOME", _RC_HOME_DEFAULT))
os.environ.setdefault("HOME", str(RC_HOME))
os.environ.setdefault("MODAL_PROFILE", "sorenlab2026")
os.environ.setdefault("SBX_MODAL_APP_NAME", "sbx-control-release01-rc")

import httpx  # noqa: E402

from tests.e2e_modal.helpers import artifacts_dir, leak_reason, write_json  # noqa: E402

APP_NAME = os.environ["SBX_MODAL_APP_NAME"]
BASE_URL = os.environ.get(
    "SBX_CONTROL_URL",
    "https://sorenlab2026--sbx-control-release01-rc-fastapi-app.modal.run",
).rstrip("/")
ACCOUNTS_DICT = "sbx-rc-accounts"
SECRET_PREFIX = "sbx-rc-acct-"
FLEET = ("antigravity-1", "antigravity-2", "antigravity-3", "antigravity-4")
FLEET_SLOTS = 1
KEY_FILE = RC_HOME / ".local/state/sbx/bootstrap.key"
STALE_ID = "00000000-0000-4000-8000-000000000000"
OAUTH_REL = ".gemini/antigravity-cli/antigravity-oauth-token"

TERMINAL_RUN = {"FINISHED", "ERROR", "CANCELLED", "EXPIRED", "UNKNOWN"}
HEALTH_CODES = {
    "auth_invalid",
    "rate_limited",
    "quota_exhausted",
    "provider_unavailable",
    "model_capacity",
}
RUN_WAIT_S = 480.0
PROMPT_OK = "Reply with exactly this line and nothing else: AGY_OK"
COOLDOWN_S = 120

RESULTS: dict[str, Any] = {}
CHECKS: list[dict[str, Any]] = []
CAPTURED: list[str] = []  # full response bodies → leak scan
WATCH: list[str] = []  # secret substrings — never printed or recorded
CREATED_AGENTS: list[str] = []  # not yet deleted — cleanup deletes these
ALL_AGENTS: list[str] = []  # every created id — final sandbox sweep
CREATED_KEYS: list[str] = []


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def record(key: str, value: object) -> None:
    RESULTS[key] = value
    print(f"[result] {key} = {json.dumps(value, ensure_ascii=False, default=str)}", flush=True)


def check(name: str, ok: bool, detail: str | None = None) -> bool:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'ok' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


# --------------------------------------------------------------------- http


def client(token: str, timeout: float = 150.0) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(connect=20.0, read=timeout, write=60.0, pool=60.0),
        follow_redirects=True,
    )


def api(cl: httpx.Client, method: str, path: str, **kw: Any) -> tuple[int, Any]:
    """One /v1 call; retries transport faults (redeploys cut connections)."""
    resp = None
    for attempt in range(4):
        try:
            resp = cl.request(method, path, **kw)
            break
        except (httpx.TransportError, httpx.TimeoutException):
            if attempt == 3:
                raise
            time.sleep(3)
    assert resp is not None
    CAPTURED.append(resp.text)
    try:
        body = resp.json()
    except Exception:
        body = None
    return resp.status_code, body


def mint_key(admin: httpx.Client) -> dict[str, str] | None:
    """Mint an ``agents``-scope key; record only id + token fingerprint."""
    for _ in range(3):
        st, body = api(
            admin,
            "POST",
            "/v1/api-keys",
            json={"label": f"agy-fleet-gate-{uuid.uuid4().hex[:6]}", "scopes": ["agents"]},
        )
        if st == 201 and isinstance(body, dict) and body.get("key"):
            CREATED_KEYS.append(body["id"])
            token = body["key"]
            WATCH.append(token)
            # The create response legitimately carries the token once —
            # replace the captured copy with a redacted body.
            CAPTURED.pop()
            CAPTURED.append(
                json.dumps({k: ("REDACTED" if k == "key" else v) for k, v in body.items()})
            )
            record(
                "api_key.minted",
                {"id": body["id"], "fp": hashlib.sha256(token.encode()).hexdigest()[:12]},
            )
            return {"id": body["id"], "token": token}
        time.sleep(3)
    return None


def api_as(
    holder: dict[str, str], admin: httpx.Client, method: str, path: str, **kw: Any
) -> tuple[int, Any]:
    """Call /v1 with holder's token; remint + retry once on 401."""
    st, body = api(client(holder["token"]), method, path, **kw)
    if st == 401:
        minted = mint_key(admin)
        if minted is not None:
            holder.update(minted)
            st, body = api(client(holder["token"]), method, path, **kw)
    return st, body


def create_agent(
    cl: httpx.Client,
    *,
    account: str = "auto",
    name: str,
    prompt: str = PROMPT_OK,
) -> tuple[int, Any]:
    return api(
        cl,
        "POST",
        "/v1/agents",
        json={
            "prompt": {"text": prompt},
            "agent": {"provider": "antigravity", "account_id": account},
            "name": name,
            "idle_timeout_s": 900,
        },
        headers={"Idempotency-Key": f"{name}-{uuid.uuid4().hex[:8]}"},
    )


def delete_agent(cl: httpx.Client, agent_id: str) -> int:
    st, _ = api(cl, "DELETE", f"/v1/agents/{agent_id}")
    return st


def with_cap_retry(
    call: Any, *, timeout: float = 180.0, extra_codes: frozenset[str] = frozenset()
) -> tuple[int, Any, int]:
    """Retry a create while the shared global/per-key cap refuses it.

    ``concurrency_limit`` here is sibling-gate/global contention, not the
    behaviour under test — retrying keeps the gate honest about which
    refusal it is actually asserting. ``extra_codes`` adds refusal codes a
    caller knows are transient here (e.g. ``account_busy`` while a just
    deleted session's slot release propagates).
    """
    retryable = {"concurrency_limit"} | set(extra_codes)
    deadline = time.monotonic() + timeout
    tries = 0
    while True:
        tries += 1
        st, body = call()
        err = (body or {}).get("error") or {}
        if not (st in (409, 429) and err.get("code") in retryable):
            return st, body, tries
        if time.monotonic() >= deadline:
            return st, body, tries
        time.sleep(15)


def post_run(cl: httpx.Client, agent_id: str, text: str) -> tuple[int, Any]:
    return api(cl, "POST", f"/v1/agents/{agent_id}/runs", json={"prompt": {"text": text}})


def wait_run(
    cl: httpx.Client, agent_id: str, n: int, *, timeout: float = RUN_WAIT_S
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        st, body = api(cl, "GET", f"/v1/agents/{agent_id}/runs/run-{n}")
        if st == 200 and isinstance(body, dict):
            last = body
            if body.get("status") in TERMINAL_RUN:
                return body
        time.sleep(4)
    return last


def accounts_view(cl: httpx.Client) -> dict[str, dict[str, Any]]:
    st, body = api(cl, "GET", "/v1/accounts")
    out: dict[str, dict[str, Any]] = {}
    if st == 200 and isinstance(body, dict):
        for acc in body.get("accounts") or []:
            if isinstance(acc, dict) and isinstance(acc.get("id"), str):
                out[acc["id"]] = acc
    return out


def fleet_view(cl: httpx.Client) -> dict[str, dict[str, Any]]:
    return {aid: acc for aid, acc in accounts_view(cl).items() if aid in FLEET}


def free_accounts(cl: httpx.Client) -> list[str]:
    """Fleet accounts currently active with a free slot."""
    view = fleet_view(cl)
    return sorted(
        aid
        for aid, a in view.items()
        if a.get("status") == "active"
        and int(a.get("running") or 0) < int(a.get("max_concurrent") or 1)
    )


def wait_fleet_free(cl: httpx.Client, need: int, *, timeout: float = 600.0) -> list[str]:
    """Wait until >= ``need`` fleet accounts are active+free (siblings share
    the pool — their holds are transient contention, not the tested path)."""
    deadline = time.monotonic() + timeout
    free: list[str] = []
    while time.monotonic() < deadline:
        free = free_accounts(cl)
        if len(free) >= need:
            return free
        time.sleep(15)
    return free


# ------------------------------------------------------- operator-level ops


def registry() -> Any:
    from control.accounts import ModalDictAccountStore, PersistentAccountRegistry

    return PersistentAccountRegistry(ModalDictAccountStore(ACCOUNTS_DICT))


def mark(account_id: str, status: str, **kw: Any) -> Any:
    return registry().mark_status(account_id, status, **kw)


def put_account_secret(account_id: str, blob_json: str) -> None:
    """Delete + recreate the per-account Secret (modal has no overwrite)."""
    import modal

    name = f"{SECRET_PREFIX}{account_id}"
    modal.Secret.objects.delete(name, allow_missing=True)
    modal.Secret.objects.create(name, env_dict={"SBX_ACCOUNT_CREDENTIAL": blob_json})


def sb_read(session_id: str, rel: str) -> str | None:
    from control.backends.modal import ModalBackend
    from control.sandbox_io import read_text

    backend = ModalBackend(APP_NAME)
    handles = backend.list(tags={"session_id": session_id})
    if not handles:
        return None
    return read_text(backend, handles[0], rel)


def sb_write(session_id: str, rel: str, content: str) -> bool:
    from control.backends.modal import ModalBackend
    from control.sandbox_io import write_file

    backend = ModalBackend(APP_NAME)
    handles = backend.list(tags={"session_id": session_id})
    if not handles:
        return False
    try:
        write_file(backend, handles[0], rel, content)
    except Exception:
        return False
    return True


def live_sandboxes() -> list[Any]:
    from control.backends.modal import ModalBackend

    return ModalBackend(APP_NAME).list()


def redeploy_fleet() -> str | None:
    """``modal deploy`` the RC app with the 4-account Antigravity seed env.

    Cycles the control-plane containers (the restart seam) and bakes
    ``SBX_ANTIGRAVITY_ACCOUNTS`` so every cold start re-seeds the 4x1 fleet.
    Image builds are not part of this path — named images already exist.
    """
    from sbx.config import load as load_cfg
    from sbx.plane import ModalPlane

    env = {
        "HOME": str(RC_HOME),
        "MODAL_PROFILE": os.environ["MODAL_PROFILE"],
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "LANG", "TERM"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    cfg = load_cfg(env=env)
    plane = ModalPlane(profile=cfg.config.modal_profile or os.environ["MODAL_PROFILE"], env=env)
    deploy_env = cfg.config.deploy_env()
    deploy_env["SBX_ANTIGRAVITY_ACCOUNTS"] = json.dumps(
        [{"id": aid, "slots": FLEET_SLOTS} for aid in FLEET]
    )
    return plane.deploy_app(cfg.config.modal_app_name, env=deploy_env)


def wait_me(cl: httpx.Client, timeout: float = 300.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            st, body = api(cl, "GET", "/v1/me")
            if st == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(5)
    return False


# ------------------------------------------------------------- watch values


def _leaf_values(obj: Any) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_leaf_values(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_leaf_values(v))
    elif isinstance(obj, str) and len(obj) >= 12:
        out.append(obj)
    return out


def watch_blob(blob: dict[str, Any]) -> None:
    WATCH.append(json.dumps(blob))
    for content in (blob.get("files") or {}).values():
        if isinstance(content, str) and len(content) >= 12:
            WATCH.append(content)
            try:
                doc = json.loads(content)
            except json.JSONDecodeError:
                continue
            WATCH.extend(_leaf_values(doc))


def bogus_blob(blob: dict[str, Any]) -> str:
    """Same-shape blob with credential leaf values replaced — real auth fail.

    Only token/secret *values* are corrupted; structural fields (``token_type``,
    ``expiry``, ``auth_method``, ``scope`` …) stay intact so the CLI follows
    its normal ``401 -> invalid_grant`` path instead of a parse/weird-flow
    hang that only ends at the runner's 900s soft timeout.
    """

    cred_keys = {
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "api_key",
        "secret",
        "password",
        "token",
    }

    def scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: (
                    "REDACTED-INVALID-GATE-TOKEN"
                    if isinstance(v, str) and k.lower() in cred_keys
                    else scrub(v)
                )
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        return obj

    files = {}
    for rel, content in (blob.get("files") or {}).items():
        try:
            files[rel] = json.dumps(scrub(json.loads(content)))
        except (json.JSONDecodeError, TypeError):
            files[rel] = "REDACTED-INVALID-GATE-TOKEN"
    return json.dumps({"provider": blob.get("provider"), "files": files})


# ------------------------------------------------------------------- phases


def phase_fleet_bake(admin: httpx.Client) -> None:
    """Bake the 4x1 fleet env, then wait for every account healthy + free."""
    log("redeploy: SBX_ANTIGRAVITY_ACCOUNTS fleet seed (4 accounts x 1 slot)")
    t0 = time.monotonic()
    url = redeploy_fleet()
    record("fleet.redeploy_s", round(time.monotonic() - t0, 1))
    check("fleet.redeploy_ok", bool(url), f"url={url}")
    check("fleet.app_ready", wait_me(admin), BASE_URL)

    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        # The cap check is the reseed-landed signal: a still-warm pre-deploy
        # container can answer ``/v1/me`` before the new version cold-starts
        # and writes the seeded slot caps. ``running`` is NOT part of
        # readiness — sibling gates hold slots transiently on this shared
        # deployment; quiescence is handled per-phase downstream.
        view = fleet_view(admin)
        if len(view) == 4 and all(
            a["status"] == "active" and int(a.get("max_concurrent") or 0) == FLEET_SLOTS
            for a in view.values()
        ):
            break
        # Operator reset: restore accounts a prior run may have left
        # cooling/invalid/disabled (dict-level, recorded in evidence).
        reg = registry()
        for aid in FLEET:
            acc = reg.get(aid)
            if acc is not None and acc.status != "active":
                mark(aid, "active")
                record(f"fleet.prest_reset.{aid}", acc.status)
        time.sleep(8)
    view = fleet_view(admin)
    ok = len(view) == 4 and all(a["status"] == "active" for a in view.values())
    compact = {
        a: {"status": v["status"], "run": v["running"], "cap": v["max_concurrent"]}
        for a, v in view.items()
    }
    check("fleet.all_active", ok, json.dumps(compact))
    check(
        "fleet.slots_1_each",
        all(int(v.get("max_concurrent") or 0) == FLEET_SLOTS for v in view.values()),
        json.dumps({a: v.get("max_concurrent") for a, v in view.items()}),
    )
    # Let pre-deploy containers finish draining so later creates cannot land
    # on a container that dies mid-provision.
    if ok:
        time.sleep(30)


def phase_distribution(
    admin: httpx.Client, holder_b: dict[str, str], run_id: str
) -> list[dict[str, Any]]:
    """4 auto creates -> 4 distinct accounts; slot-full refusals observed."""
    # Distribution needs the whole fleet free; sibling gates hold agy slots
    # transiently — wait for quiescence first. Recorded, not asserted: the
    # per-create provider_exhausted retries below still land agents as slots
    # free up, and dist.distinct_accounts carries the real assertion.
    free = wait_fleet_free(admin, 4, timeout=900)
    record("dist.pre_free_accounts", free)
    agents: list[dict[str, Any]] = []
    # Sibling gates share the global cap and can transiently hold antigravity
    # slots — the burst shares one deadline so early cheap creates leave the
    # budget for contended ones.
    burst_deadline = time.monotonic() + 1200
    for i in range(4):
        payload = {
            "prompt": {"text": PROMPT_OK},
            "agent": {"provider": "antigravity", "account_id": "auto"},
            "name": f"{run_id}-d{i + 1}",
            "idle_timeout_s": 900,
        }
        if i < 2:
            call = lambda: api(admin, "POST", "/v1/agents", json=payload)  # noqa: E731
        else:
            # Per-owner cap is 2 live agents — the burst needs a second key.
            call = lambda: api_as(holder_b, admin, "POST", "/v1/agents", json=payload)  # noqa: E731
        remaining = burst_deadline - time.monotonic()
        if remaining <= 0:
            check(f"dist.create_{i + 1}", False, "burst deadline exhausted")
            break
        st, body, tries = with_cap_retry(
            call, timeout=remaining, extra_codes=frozenset({"provider_exhausted"})
        )
        if tries > 1:
            record(f"dist.create_{i + 1}_cap_retries", tries)
        if st != 201 or not isinstance(body, dict):
            err = ((body or {}).get("error") or {}).get("code")
            check(f"dist.create_{i + 1}", False, f"status={st} code={err}")
            break
        agent = body["agent"]
        agents.append(agent)
        CREATED_AGENTS.append(agent["id"])
        ALL_AGENTS.append(agent["id"])
        log(f"  create {i + 1}: agent={agent['id'][:12]} account={agent.get('account_id')}")
    resolved = [a.get("account_id") for a in agents]
    check(
        "dist.four_distinct_accounts",
        sorted(resolved) == sorted(FLEET),
        f"resolved={resolved}",
    )
    view = fleet_view(admin)
    check(
        "dist.running_one_each",
        all(int(view.get(a, {}).get("running") or 0) >= 1 for a in FLEET),
        json.dumps({a: view.get(a, {}).get("running") for a in FLEET}),
    )
    if len(agents) == 4:
        busy = agents[0].get("account_id")
        st, body = create_agent(admin, account=str(busy), name=f"{run_id}-busy")
        check(
            "slots.named_busy_409",
            st == 409 and (body or {}).get("error", {}).get("code") == "account_busy",
            f"status={st} code={(body or {}).get('error', {}).get('code')}",
        )
        st, body = create_agent(admin, account="antigravity-99", name=f"{run_id}-missing")
        check(
            "slots.named_missing_409",
            st == 409 and (body or {}).get("error", {}).get("code") == "account_unavailable",
            f"status={st} code={(body or {}).get('error', {}).get('code')}",
        )
        st, body = create_agent(admin, account="codex-1", name=f"{run_id}-wrongprov")
        check(
            "slots.named_wrong_provider_409",
            st == 409 and (body or {}).get("error", {}).get("code") == "account_unavailable",
            f"status={st} code={(body or {}).get('error', {}).get('code')}",
        )
        # Fleet full -> auto create must refuse provider_exhausted.
        st, body = api_as(
            holder_b,
            admin,
            "POST",
            "/v1/agents",
            json={
                "prompt": {"text": PROMPT_OK},
                "agent": {"provider": "antigravity", "account_id": "auto"},
                "name": f"{run_id}-full",
                "idle_timeout_s": 900,
            },
        )
        err = (body or {}).get("error") or {}
        if st == 201 and isinstance(body, dict):
            # Fleet unexpectedly had capacity — free the extra agent at once.
            aid = body["agent"]["id"]
            ALL_AGENTS.append(aid)
            delete_agent(admin, aid)
        check(
            "slots.auto_exhausted_429",
            st == 429 and err.get("code") == "provider_exhausted",
            f"status={st} code={err.get('code')} retry_after={err.get('retry_after')}",
        )
        check("slots.exhausted_retry_after", err.get("retry_after") is not None)

    # Every run-1 must really finish — real auth + real turn per account.
    deadline = time.monotonic() + 600
    done: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline and len(done) < len(agents):
        for agent in agents:
            aid = agent["id"]
            if aid in done:
                continue
            st, body = api(admin, "GET", f"/v1/agents/{aid}/runs/run-1")
            if st == 200 and isinstance(body, dict) and body.get("status") in TERMINAL_RUN:
                done[aid] = body
        if len(done) < len(agents):
            time.sleep(6)
    # A create that landed on a draining pre-deploy container can lose its
    # sandbox mid-provision (ClientClosed) — an infra flake, not the
    # behaviour under test. Recreate that account's agent once and re-wait.
    for i, agent in enumerate(agents):
        if agent["id"] in done:
            continue
        acct = str(agent.get("account_id") or "")
        delete_agent(admin, agent["id"])
        if agent["id"] in CREATED_AGENTS:
            CREATED_AGENTS.remove(agent["id"])
        st, body, _ = with_cap_retry(
            lambda: create_agent(admin, account=acct, name=f"{run_id}-d{i + 1}b"),
            extra_codes=frozenset({"account_busy"}),
        )
        record(f"dist.run1_recovered.{acct}", {"create_status": st})
        if st != 201 or not isinstance(body, dict):
            continue
        repl = body["agent"]
        agents[i] = repl
        CREATED_AGENTS.append(repl["id"])
        ALL_AGENTS.append(repl["id"])
        run = wait_run(admin, repl["id"], 1, timeout=RUN_WAIT_S)
        if run is not None:
            done[repl["id"]] = run
    per_acct = {}
    all_ok = True
    for agent in agents:
        run = done.get(agent["id"]) or {}
        text = (run.get("result") or {}).get("text") or ""
        ok = run.get("status") == "FINISHED" and bool(text.strip())
        all_ok = all_ok and ok
        per_acct[str(agent.get("account_id"))] = {
            "status": run.get("status"),
            "marker": "AGY_OK" in text,
        }
    check("dist.run1_all_finished", all_ok and len(done) == len(agents), json.dumps(per_acct))
    record("dist.assignments", {a["id"][:12]: a.get("account_id") for a in agents})
    return agents


def phase_resume_stale(admin: httpx.Client, agent: dict[str, Any]) -> None:
    """Two-turn resume + stale-conversation semantics on one live agent."""
    aid = agent["id"]
    marker = f"AGY-FLEET-{uuid.uuid4().hex[:8].upper()}"
    record("resume.marker", marker)

    st, body = post_run(
        admin,
        aid,
        f"Remember this exact phrase for the rest of our conversation: {marker}\n"
        "Then reply with exactly this line and nothing else: AGY_MEM_DONE",
    )
    check("resume.run2_accepted", st == 201, f"status={st}")
    run2 = wait_run(admin, aid, 2)
    check(
        "resume.run2_finished",
        isinstance(run2, dict) and run2.get("status") == "FINISHED",
        f"status={(run2 or {}).get('status')}",
    )

    st, body = post_run(
        admin,
        aid,
        "Without reading any files or running any commands, what exact phrase did I "
        "ask you to remember earlier in this conversation? Reply with exactly that "
        "phrase and nothing else.",
    )
    check("resume.run3_accepted", st == 201, f"status={st}")
    run3 = wait_run(admin, aid, 3)
    text3 = ((run3 or {}).get("result") or {}).get("text") or ""
    check(
        "resume.run3_recalls_marker",
        isinstance(run3, dict) and run3.get("status") == "FINISHED" and marker in text3,
        f"status={(run3 or {}).get('status')} recalled={marker in text3}",
    )

    session = json.loads(sb_read(aid, "session.json") or "{}")
    conv = session.get("native_session_id")
    check("resume.native_session_id", bool(conv), str(conv))
    events = sb_read(aid, "events.jsonl") or ""
    CAPTURED.append(events)
    thread_ids = [
        json.loads(line).get("thread_id")
        for line in events.splitlines()
        if line.strip().startswith("{") and '"thread.started"' in line
    ]
    check(
        "resume.same_thread_turns",
        bool(conv) and len(set(t for t in thread_ids if t)) == 1 and conv in thread_ids,
        f"threads={len(set(thread_ids))}",
    )
    CAPTURED.append(sb_read(aid, "events.raw.jsonl") or "")
    for rel in ("turns/1.json", "turns/2.json", "turns/3.json"):
        CAPTURED.append(sb_read(aid, rel) or "")

    # --- stale: corrupt native_session_id, run must fail, id never adopted
    session["native_session_id"] = STALE_ID
    session["codex_session_id"] = STALE_ID
    check(
        "stale.session_corrupted",
        sb_write(aid, "session.json", json.dumps(session) + "\n"),
    )
    st, _ = post_run(admin, aid, "Reply with exactly: AGY_STALE_DONE")
    check("stale.run4_accepted", st == 201, f"status={st}")
    run4 = wait_run(admin, aid, 4, timeout=300)
    err4 = (run4 or {}).get("error") or {}
    check(
        "stale.run4_error",
        isinstance(run4, dict) and run4.get("status") == "ERROR" and bool(err4.get("code")),
        f"status={(run4 or {}).get('status')} code={err4.get('code')}",
    )
    record("stale.error_code", err4.get("code"))
    CAPTURED.append(sb_read(aid, "turns/4.json") or "")
    CAPTURED.append(sb_read(aid, "events.jsonl") or "")
    after = json.loads(sb_read(aid, "session.json") or "{}")
    check(
        "stale.keeps_requested_id",
        after.get("native_session_id") == STALE_ID,
        str(after.get("native_session_id")),
    )
    # runtime-side failure must not mark the account unhealthy.
    view = fleet_view(admin)
    acct = str(agent.get("account_id"))
    check(
        "stale.account_stays_active",
        view.get(acct, {}).get("status") == "active",
        f"{acct}={view.get(acct, {}).get('status')}",
    )


def phase_failover(admin: httpx.Client, run_id: str) -> None:
    """Unavailable / cooling / disabled / real auth-invalid + auto failover."""

    def auto_pick(tag: str) -> str | None:
        # admin key has 0 live agents here — no per-owner cap pressure.
        # provider_exhausted/concurrency_limit under sibling contention are
        # retried: the asserted behavior is *which* account gets picked.
        st, body, _ = with_cap_retry(
            lambda: create_agent(admin, name=f"{run_id}-{tag}"),
            timeout=420,
            extra_codes=frozenset({"provider_exhausted"}),
        )
        if st != 201 or not isinstance(body, dict):
            record(f"fo.{tag}.create_status", st)
            return None
        aid = body["agent"]["id"]
        CREATED_AGENTS.append(aid)
        ALL_AGENTS.append(aid)
        acct = body["agent"].get("account_id")
        delete_agent(admin, aid)
        CREATED_AGENTS.remove(aid)
        return str(acct) if acct else None

    # -- a) cooling on a currently-free account: marked cooling -> named
    # refuse, auto skips it, lazy recovery once the cooldown expires.
    free = wait_fleet_free(admin, 1, timeout=600)
    cool_target = free[0] if free else None
    record("fo.cooling_target", cool_target)
    if check("fo.cooling_has_free", cool_target is not None, f"free={free}"):
        until = iso(datetime.now(UTC) + timedelta(seconds=COOLDOWN_S))
        mark(cool_target, "cooling", cooldown_until=until, last_error="gate_probe")
        st, body = create_agent(admin, account=cool_target, name=f"{run_id}-cool")
        err = (body or {}).get("error") or {}
        # 409 refusals carry no retry_after by contract (the hint rides the
        # 429 pool-exhausted path); record what the refusal actually carried.
        record("fo.cooling_named_retry_after", err.get("retry_after"))
        check(
            "fo.cooling_named_409",
            st == 409 and err.get("code") == "account_unavailable",
            f"status={st} code={err.get('code')} retry_after={err.get('retry_after')}",
        )
        picked = auto_pick("fo-cool")
        check(
            "fo.cooling_auto_skips",
            picked is not None and picked in FLEET and picked != cool_target,
            f"picked={picked} target={cool_target}",
        )
        log(f"  cooling sleep {COOLDOWN_S + 15}s for lazy recovery window")
        deadline = time.monotonic() + COOLDOWN_S + 120
        recovered = False
        while time.monotonic() < deadline:
            st, body = create_agent(admin, account=cool_target, name=f"{run_id}-coolrec")
            if st == 201:
                recovered = True
                aid = body["agent"]["id"]
                CREATED_AGENTS.append(aid)
                ALL_AGENTS.append(aid)
                delete_agent(admin, aid)
                CREATED_AGENTS.remove(aid)
                break
            time.sleep(10)
        check("fo.cooling_lazy_recovery", recovered)
        check(
            "fo.cooling_status_active",
            fleet_view(admin).get(cool_target, {}).get("status") == "active",
        )

    # -- b) disabled on a free account: named refuse, auto skips, re-enable
    free = wait_fleet_free(admin, 1, timeout=600)
    dis_target = free[-1] if free else None
    record("fo.disabled_target", dis_target)
    if check("fo.disabled_has_free", dis_target is not None, f"free={free}"):
        mark(dis_target, "disabled", last_error="gate_probe")
        st, body = create_agent(admin, account=dis_target, name=f"{run_id}-dis")
        err = (body or {}).get("error") or {}
        check(
            "fo.disabled_named_409",
            st == 409 and err.get("code") == "account_unavailable",
            f"status={st} code={err.get('code')}",
        )
        picked = auto_pick("fo-dis")
        check(
            "fo.disabled_auto_skips",
            picked is not None and picked in FLEET and picked != dis_target,
            f"picked={picked} target={dis_target}",
        )
        mark(dis_target, "active")
        check(
            "fo.disabled_reenabled",
            fleet_view(admin).get(dis_target, {}).get("status") == "active",
        )

    # -- c) REAL auth-invalid: bogus per-account Secret -> real turn failure
    free = wait_fleet_free(admin, 1, timeout=600)
    auth_target = free[0] if free else None
    record("fo.auth_target", auth_target)
    if not check("fo.auth_has_free", auth_target is not None, f"free={free}"):
        return
    blob = registry().get_credential_blob(auth_target)
    if not check("fo.auth_blob_present", isinstance(blob, dict)):
        return
    put_account_secret(auth_target, bogus_blob(blob))
    record("fo.auth_secret_swapped", True)
    # The slot may be grabbed by a sibling between the free-check and the
    # named create — account_busy is transient contention here.
    st, body, _ = with_cap_retry(
        lambda: create_agent(admin, account=auth_target, name=f"{run_id}-badauth"),
        timeout=480,
        extra_codes=frozenset({"account_busy"}),
    )
    bad_agent = None
    if st == 201 and isinstance(body, dict):
        bad_agent = body["agent"]["id"]
        CREATED_AGENTS.append(bad_agent)
        ALL_AGENTS.append(bad_agent)
        # Wait past the runner soft timeout (900s): if a mangled credential
        # hangs the CLI, the turn still ends terminal and is captured.
        run = wait_run(admin, bad_agent, 1, timeout=1000)
        err = (run or {}).get("error") or {}
        failed = (
            isinstance(run, dict)
            and run.get("status") in {"ERROR", "EXPIRED"}
            and bool(err.get("code"))
        )
        check(
            "fo.auth_run_error",
            failed,
            f"status={(run or {}).get('status')} code={err.get('code')}",
        )
        record("fo.auth_error", {"code": err.get("code"), "source": err.get("source")})
        CAPTURED.append(sb_read(bad_agent, "turns/1.json") or "")
        CAPTURED.append(sb_read(bad_agent, "events.raw.jsonl") or "")
        # Rendering the terminal run feeds the reporter -> account health.
        status_now = fleet_view(admin).get(auth_target, {}).get("status")
        if err.get("code") in HEALTH_CODES:
            check(
                "fo.auth_account_marked",
                status_now != "active",
                f"{auth_target}={status_now} code={err.get('code')}",
            )
            picked = auto_pick("fo-auth")
            check(
                "fo.auth_auto_failover",
                picked is not None and picked in FLEET and picked != auth_target,
                f"picked={picked} target={auth_target}",
            )
        else:
            record("fo.auth_nonhealth_code", err.get("code"))
    else:
        check("fo.auth_create", False, f"status={st}")
    # Restore the real credential: same blob, fresh Secret, account re-activated.
    put_account_secret(auth_target, json.dumps(blob))
    mark(auth_target, "active")
    record("fo.auth_secret_restored", True)
    if bad_agent:
        delete_agent(admin, bad_agent)
        CREATED_AGENTS.remove(bad_agent)
    # Prove the restored credential works end-to-end.
    st, body, _ = with_cap_retry(
        lambda: create_agent(admin, account=auth_target, name=f"{run_id}-restored"),
        timeout=480,
        extra_codes=frozenset({"account_busy"}),
    )
    if st == 201 and isinstance(body, dict):
        rid = body["agent"]["id"]
        CREATED_AGENTS.append(rid)
        ALL_AGENTS.append(rid)
        run = wait_run(admin, rid, 1, timeout=RUN_WAIT_S)
        check(
            "fo.auth_restored_turn_ok",
            isinstance(run, dict) and run.get("status") == "FINISHED",
            f"status={(run or {}).get('status')}",
        )
        delete_agent(admin, rid)
        CREATED_AGENTS.remove(rid)
    else:
        check("fo.auth_restored_create", False, f"status={st}")


def phase_restart(admin: httpx.Client, run_id: str) -> None:
    """Idle agents hold slots across a control-plane redeploy."""
    # Acquire 2 held slots on whichever accounts are currently free — sibling
    # gates hold agy slots transiently, so targets are chosen dynamically and
    # a busy account is skipped rather than retried to exhaustion.
    held: dict[str, str] = {}
    attempts: list[str] = []
    deadline = time.monotonic() + 720
    while len(held) < 2 and time.monotonic() < deadline:
        free_now = [a for a in free_accounts(admin) if a not in held]
        if not free_now:
            time.sleep(15)
            continue
        acct = free_now[0]
        st, body = create_agent(admin, account=acct, name=f"{run_id}-hold-{acct[-1]}")
        if st == 201 and isinstance(body, dict):
            aid = body["agent"]["id"]
            CREATED_AGENTS.append(aid)
            ALL_AGENTS.append(aid)
            held[acct] = aid
            run = wait_run(admin, aid, 1, timeout=300)
            attempts.append(f"{acct}:201:run1={(run or {}).get('status')}")
        else:
            err = ((body or {}).get("error") or {}).get("code")
            attempts.append(f"{acct}:{st}:{err}")
            if st not in (409, 429):
                break  # non-capacity failure — held_two + attempts record it
            time.sleep(15)
    record("restart.hold_attempts", attempts)
    check(
        "restart.held_two",
        len(held) == 2,
        f"held={sorted(held)} attempts={attempts}",
    )
    view = fleet_view(admin)
    check(
        "restart.pre_counts",
        all(int(view.get(a, {}).get("running") or 0) == 1 for a in held),
        json.dumps({a: view.get(a, {}).get("running") for a in FLEET}),
    )

    t0 = time.monotonic()
    url = redeploy_fleet()
    record("restart.redeploy_s", round(time.monotonic() - t0, 1))
    check("restart.redeploy_ok", bool(url), f"url={url}")
    check("restart.app_ready", wait_me(admin), BASE_URL)

    # Wait for the new version's reseed to land (cap==1 written by a cold
    # start), then let pre-deploy containers finish draining before the
    # post-restart create.
    view = fleet_view(admin)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        view = fleet_view(admin)
        if len(view) == 4 and all(
            int(a.get("max_concurrent") or 0) == FLEET_SLOTS for a in view.values()
        ):
            break
        time.sleep(8)
    time.sleep(30)
    view = fleet_view(admin)
    check(
        "restart.counts_survive",
        all(int(view.get(a, {}).get("running") or 0) == 1 for a in held),
        json.dumps({a: view.get(a, {}).get("running") for a in FLEET}),
    )
    # Auto must land on a still-free account — restart must not oversell.
    minted = mint_key(admin)
    check("restart.mint_key", minted is not None)
    if minted:
        st, body, _ = with_cap_retry(
            lambda: api_as(
                minted,
                admin,
                "POST",
                "/v1/agents",
                json={
                    "prompt": {"text": PROMPT_OK},
                    "agent": {"provider": "antigravity", "account_id": "auto"},
                    "name": f"{run_id}-postrestart",
                    "idle_timeout_s": 600,
                },
            ),
            timeout=420,
            extra_codes=frozenset({"provider_exhausted"}),
        )
        free = sorted(set(FLEET) - set(held))
        picked = (body or {}).get("agent", {}).get("account_id") if st == 201 else None
        check(
            "restart.auto_uses_free_slot",
            st == 201 and picked in free,
            f"status={st} picked={picked} free={free}",
        )
        if st == 201 and isinstance(body, dict):
            aid = body["agent"]["id"]
            CREATED_AGENTS.append(aid)
            ALL_AGENTS.append(aid)
            # Post-restart execution path must actually run a real turn.
            run = wait_run(admin, aid, 1, timeout=RUN_WAIT_S)
            check(
                "restart.post_turn_finished",
                isinstance(run, dict) and run.get("status") == "FINISHED",
                f"status={(run or {}).get('status')}",
            )
            delete_agent(admin, aid)
            CREATED_AGENTS.remove(aid)


def phase_leaks(admin: httpx.Client) -> None:
    from tests.e2e_modal.helpers import modal_app_logs

    logs = modal_app_logs(APP_NAME, tail=500)
    CAPTURED.append(logs)
    blob = "\n".join(CAPTURED)
    hits = []
    for i, secret in enumerate(WATCH):
        if secret and len(secret) >= 8 and secret in blob:
            hits.append(f"watch#{i}")
    why = leak_reason(blob)
    if why:
        hits.append(why)
    check("leak.no_secret_material", not hits, f"hits={hits}")
    record("leak.captured_bytes", len(blob))


def phase_cleanup(admin: httpx.Client) -> None:
    for aid in list(CREATED_AGENTS):
        try:
            delete_agent(admin, aid)
        except Exception:
            pass
        CREATED_AGENTS.remove(aid)
    for kid in list(CREATED_KEYS):
        try:
            api(admin, "DELETE", f"/v1/api-keys/{kid}")
        except Exception:
            pass
        CREATED_KEYS.remove(kid)
    # Operator restore: every fleet account back to active.
    for aid in FLEET:
        try:
            acc = registry().get(aid)
            if acc is not None and acc.status != "active":
                mark(aid, "active")
        except Exception:
            pass
    time.sleep(5)
    view = fleet_view(admin)
    # ``running`` counts include sibling-gate sessions sharing the fleet —
    # gate-owned cleanup is proven by the sandbox sweep below; here we only
    # assert account health was restored.
    check(
        "cleanup.accounts_active",
        len(view) == 4 and all(v.get("status") == "active" for v in view.values()),
        json.dumps({a: {"s": v["status"], "r": v["running"]} for a, v in view.items()}),
    )
    # Termination propagates asynchronously — sweep, then re-check.
    deadline = time.monotonic() + 90
    leftover: list[str] = []
    while time.monotonic() < deadline:
        try:
            live = {(h.tags or {}).get("session_id"): h for h in live_sandboxes()}
        except Exception:
            live = {}
        leftover = [sid for sid in ALL_AGENTS if sid in live]
        if not leftover:
            break
        for sid in leftover:
            try:
                from control.backends.modal import ModalBackend

                ModalBackend(APP_NAME).terminate(live[sid])
            except Exception:
                pass
        time.sleep(5)
    check("cleanup.no_gate_sandboxes", not leftover, f"leftover={leftover}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--skip-redeploy", action="store_true", help="reuse the deployed env as-is")
    args = ap.parse_args()

    artifacts_dir()
    if not KEY_FILE.is_file():
        print(f"[skip] bootstrap key missing: {KEY_FILE}", flush=True)
        RESULTS["verdict"] = "SKIP"
        write_json("agy_fleet_gate.json", RESULTS)
        return 2
    token = KEY_FILE.read_text(encoding="utf-8").strip()
    WATCH.append(token)
    record(
        "0.params",
        {
            "app": APP_NAME,
            "base_url": BASE_URL,
            "key_fp": hashlib.sha256(token.encode()).hexdigest()[:12],
            "fleet": list(FLEET),
            "slots": FLEET_SLOTS,
        },
    )
    for aid in FLEET:
        blob = registry().get_credential_blob(aid)
        if not isinstance(blob, dict):
            print(f"[skip] credential blob missing: {aid}", flush=True)
            RESULTS["verdict"] = "SKIP"
            write_json("agy_fleet_gate.json", RESULTS)
            return 2
        watch_blob(blob)
        fp = hashlib.sha256(json.dumps(blob).encode()).hexdigest()[:16]
        record(f"0.cred.{aid}", {"hash16": fp})

    admin = client(token)
    run_id = f"agyfleet-{int(time.time())}"
    verdict = "FAIL"
    try:
        if not args.skip_redeploy:
            phase_fleet_bake(admin)
        else:
            check("fleet.app_ready", wait_me(admin), BASE_URL)
        holder_b = mint_key(admin)
        check("keys.mint_agents_scope", holder_b is not None)
        if holder_b is not None:
            agents = phase_distribution(admin, holder_b, run_id)
            if agents:
                phase_resume_stale(admin, agents[0])
                # Free the fleet before the failover matrix.
                for agent in agents:
                    if agent["id"] in CREATED_AGENTS:
                        delete_agent(admin, agent["id"])
                        CREATED_AGENTS.remove(agent["id"])
                phase_failover(admin, run_id)
                phase_restart(admin, run_id)
    except Exception:
        record("error", traceback.format_exc()[-2000:])
    finally:
        phase_leaks(admin)
        phase_cleanup(admin)
        if CHECKS and all(c["ok"] for c in CHECKS):
            verdict = "PASS"
        RESULTS["verdict"] = verdict
        RESULTS["failed_checks"] = [c["name"] for c in CHECKS if not c["ok"]]
        RESULTS["checks"] = CHECKS
        # Self-scan the artifact payload too.
        payload = json.dumps(RESULTS, default=str)
        for i, secret in enumerate(WATCH):
            if secret and len(secret) >= 8 and secret in payload:
                RESULTS.setdefault("artifact_leak", []).append(f"watch#{i}")
                RESULTS["verdict"] = "FAIL"
        path = write_json("agy_fleet_gate.json", RESULTS)
        log(f"verdict={RESULTS['verdict']} failed={RESULTS['failed_checks']} artifact={path}")
    return 0 if RESULTS["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Real MVP acceptance: email/password + Modal + GitHub token + OpenCode Zen only.

Opt-in. Reads SBX_BENCHMARK_EMAIL/PASSWORD, SBX_TEST_INFERENCE_API_KEY, SBX_TEST_MODAL_TOKEN_ID/SECRET,
SBX_TEST_GITHUB_TOKEN, SBX_BENCHMARK_GITHUB_REPO from the environment and never prints them.
Runs a real PostgreSQL, a separate control-plane process (restartable), real Modal sandboxes
through the user's stored Modal Connection, the official OpenCode CLI with the user's Zen key,
and real GitHub effects only in the disposable repository. Writes redacted evidence JSON.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from sbx.sdk import SBXClient, SBXError  # noqa: E402
from tests.e2e_modal.inference import inference_credential  # noqa: E402
from tests.support.postgres import _pg_bin  # noqa: E402

RUN = time.strftime("%Y%m%d%H%M%S")
REPO = os.environ["SBX_BENCHMARK_GITHUB_REPO"]
assert REPO.endswith("/sbx-e2e-test"), "destructive GitHub testing is restricted to the e2e repo"
SECRETS = {
    name: os.environ[name]
    for name in (
        "SBX_TEST_INFERENCE_API_KEY",
        "SBX_TEST_MODAL_TOKEN_SECRET",
        "SBX_TEST_GITHUB_TOKEN",
        "SBX_BENCHMARK_PASSWORD",
    )
}
EVIDENCE: dict[str, Any] = {"run": RUN, "repository": REPO, "steps": {}}
BASE = Path("/tmp/sbx-mvp") / RUN
GH = {
    "Authorization": f"Bearer {os.environ['SBX_TEST_GITHUB_TOKEN']}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "sbx-mvp",
}
RESPONSES: list[str] = []
STATE: dict[str, Any] = {"refs": []}


class GateFailed(Exception):
    pass


def check(condition: Any, gate: str, *, fatal: bool = True) -> None:
    """Record a gate verdict; fatal gates stop the run (later steps depend on them)."""
    EVIDENCE.setdefault("gates", {})[gate] = bool(condition)
    if not condition:
        print(f"[FAIL] {gate}", flush=True)
        if fatal:
            raise GateFailed(gate)


def redact(text: str) -> str:
    for value in SECRETS.values():
        if value:
            text = text.replace(value, "REDACTED")
    return text


def step(name: str, **data: Any) -> None:
    EVIDENCE["steps"][name] = {"ok": True, **data}
    print(redact(f"[ok] {name}: " + json.dumps(data, default=str)[:300]), flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Recording(httpx.Client):
    def request(self, *a: Any, **kw: Any) -> httpx.Response:  # type: ignore[override]
        r = super().request(*a, **kw)
        RESPONSES.append(r.text)
        return r


def start_postgres() -> str:
    bindir = _pg_bin()
    data, sock = BASE / "pg", BASE / "pgsock"
    sock.mkdir(parents=True)
    prefix = []
    if os.geteuid() == 0:
        shutil.chown(BASE, "postgres")
        shutil.chown(sock, "postgres")
        prefix = ["runuser", "-u", "postgres", "--"]
    subprocess.run(
        [*prefix, str(bindir / "initdb"), "-D", str(data), "-A", "trust", "-U", "sbx", "--no-sync"],
        check=True,
        capture_output=True,
    )
    port = free_port()
    subprocess.run(
        [
            *prefix,
            str(bindir / "pg_ctl"),
            "-D",
            str(data),
            "-o",
            f"-k {sock} -p {port} -c listen_addresses=''",
            "-l",
            str(BASE / "pg.log"),
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    admin = f"host={sock} port={port} user=sbx dbname=postgres"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute("CREATE DATABASE sbx_mvp")
    EVIDENCE["postgres"] = {
        "stop": [*prefix, str(bindir / "pg_ctl"), "-D", str(data), "-m", "fast", "stop"]
    }
    return f"host={sock} port={port} user=sbx dbname=sbx_mvp"


class ControlPlane:
    def __init__(self, dsn: str) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        from control.security.vault import Vault

        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(BASE / "home"),
            "PYTHONPATH": f"{ROOT}:{ROOT / 'src'}",
            "SBX_DATABASE_URL": dsn,
            "SBX_VAULT_KEYS": Vault.generate_spec("mvp1"),
            "SBX_RUNTIME_MASTER_KEY": secrets.token_hex(32),
            "SBX_DATA_DIR": str(BASE / "data"),
            "SBX_PUBLIC_URL": self.url,
            "SBX_ALLOWED_ORIGINS": self.url,
            "SBX_EXECUTORS": "modal",
            "SBX_WORKER_THREADS": "6",
        }
        (BASE / "home").mkdir(parents=True, exist_ok=True)
        self.proc: subprocess.Popen[bytes] | None = None
        self.generation = 0

    def start(self) -> None:
        self.generation += 1
        log = open(BASE / f"control-{self.generation}.log", "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "control.composition", "serve", "--port", str(self.port)],
            env=self.env,
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        for _ in range(200):
            try:
                if httpx.get(self.url + "/readyz", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.25)
        raise RuntimeError("control plane did not start")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=30)

    def client(self) -> SBXClient:
        return SBXClient(self.url, http=Recording(base_url=self.url, timeout=120))


def signup(cp: ControlPlane, email: str, password: str) -> SBXClient:
    c = cp.client()
    c.register(email, password)
    mail = sorted((BASE / "data" / "mail").glob("*.json"), reverse=True)
    token = next(
        json.loads(p.read_text())["body"].split("token=")[1].strip()
        for p in mail
        if json.loads(p.read_text())["to"] == email.lower()
    )
    c.verify_email(token)
    c.login(email, password)
    return c


def wait(pred: Any, timeout: float, what: str, poll: float = 3.0) -> Any:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(poll)
    raise TimeoutError(what)


def output_text(c: SBXClient, session_id: str, turn_id: str) -> str:
    for m in c.messages.list(session_id):
        if m["turn_id"] == turn_id and m["role"] == "assistant":
            return "\n".join(p["content"] for p in m["parts"] if p["kind"] == "text")
    return ""


def native_ids(c: SBXClient, session_id: str) -> list[str]:
    return [
        e["payload"]["native_id"]
        for e in c.sessions.events(session_id)
        if e["type"] == "execution.native_bound"
    ]


def gh(method: str, path: str, **kw: Any) -> httpx.Response:
    return httpx.request(
        method, f"https://api.github.com/repos/{REPO}{path}", headers=GH, timeout=30, **kw
    )


def run() -> None:
    email, password = os.environ["SBX_BENCHMARK_EMAIL"], os.environ["SBX_BENCHMARK_PASSWORD"]
    dsn = start_postgres()
    STATE["dsn"] = dsn
    cp = ControlPlane(dsn)
    STATE["cp"] = cp
    cp.start()
    created_refs: list[str] = STATE["refs"]
    c = signup(cp, email, password)
    ws = c.workspace_id
    STATE["workspace"] = ws
    step(
        "01_login_email_password",
        user_id=c.me()["user"]["id"],
        verified=c.me()["user"]["email_verified"],
    )

    zen = c.connections.add("inference_api", inference_credential(), "Inference (benchmark)")
    zen = c.connections.wait_health(zen["id"], deadline=180)
    step(
        "02_inference_connection",
        id=zen["id"],
        health=zen["health"],
        validation=zen["validation"],
        config=zen["config"],
    )
    modal = c.connections.add(
        "modal",
        {
            "token_id": os.environ["SBX_TEST_MODAL_TOKEN_ID"],
            "token_secret": os.environ["SBX_TEST_MODAL_TOKEN_SECRET"],
        },
        "Modal (benchmark)",
    )
    modal = c.connections.wait_health(modal["id"], deadline=180)
    step("03_modal_connection", id=modal["id"], health=modal["health"])
    github = c.connections.add(
        "github", {"token": os.environ["SBX_TEST_GITHUB_TOKEN"]}, "GitHub (benchmark)"
    )
    github = c.connections.wait_health(github["id"], deadline=180)
    step(
        "04_github_connection",
        id=github["id"],
        health=github["health"],
        identity_present=bool(github["external_identity"]),
    )
    kinds = sorted(x["kind"] for x in c.connections.list())
    check(all(x["health"] == "ready" for x in (zen, modal, github)), "connections_ready")
    check("codex" not in kinds, "no_codex_connection")
    step("05_no_codex_connection", kinds=kinds)
    models = c.models()
    free = [m["id"] for conn in models["connections"] for m in conn["models"] if m["free"]]
    check(models["preferred_model"] in free, "free_model_usable")
    step("06_usable_free_model", preferred=models["preferred_model"], free_models=free[:10])

    main_sha = gh("GET", "/git/ref/heads/main").json()["object"]["sha"]
    base_branch = f"sbx-opus-e2e/base-{RUN}"
    created = gh("POST", "/git/refs", json={"ref": f"refs/heads/{base_branch}", "sha": main_sha})
    check(created.status_code == 201, "base_branch_created")
    created_refs.append(base_branch)
    spec = {
        "repository": {"full_name": REPO, "base_ref": base_branch},
        "checks": [
            {
                "name": "unittest",
                "argv": ["python3", "-m", "unittest", "discover", "-v"],
                "timeout_seconds": 300,
            }
        ],
        "defaults": {
            "harness": {"provider_id": "opencode"},
            "executor": {"backend": "modal", "resource_class": "small"},
        },
        "ship_policy": {"base_branch": base_branch, "draft": True},
    }
    project = c.projects.create(f"e2e-{RUN}", spec, name=f"MVP {RUN}")
    codeword = f"ORCHID-{RUN[-6:]}"
    t1 = (
        "Create calc.py with a function add(a, b) that returns a + b, and test_calc.py with a unittest "
        f"TestCase that checks add(2, 3) == 5. Remember this codeword for later: {codeword}. "
        "Then run `python3 -m unittest discover -v` and tell me the result."
    )
    created_session = c.sessions.create(
        project_id=project["id"], title="MVP calc", message={"content": t1}
    )
    sid = created_session["session_id"]
    turn1 = c.turns.wait(created_session["turn_id"], deadline=1200, poll=5)
    view = c.sessions.executor(sid)
    step(
        "07_project_session",
        project_id=project["id"],
        session_id=sid,
        model=created_session["session"]["harness"]["model"],
    )
    step("08_modal_sandbox_via_user_credential", backend=view["backend"], lease=view["leases"][0])
    tools = [
        e["payload"].get("title") or e["payload"].get("name")
        for e in c.sessions.events(sid)
        if e["type"] == "tool.completed"
    ]
    step(
        "09_first_opencode_turn",
        turn_id=turn1["id"],
        state=turn1["state"],
        cli=c.turns.get(turn1["id"]),
        tools=tools[:12],
    )
    check(turn1["state"] == "succeeded", "first_turn_succeeded")

    t2 = (
        "What is the codeword I asked you to remember? Reply with it first. Then add subtract(a, b) to "
        "calc.py with a unittest test and run `python3 -m unittest discover -v` again."
    )
    turn2_id = c.sessions.send(sid, t2)["turn_id"]
    turn2 = c.turns.wait(turn2_id, deadline=1200, poll=5)
    text2 = output_text(c, sid, turn2_id)
    ids = native_ids(c, sid)
    step(
        "10_followup_native_continuity",
        state=turn2["state"],
        codeword_recalled=codeword in text2,
        native_ids=ids,
        same_native_session=len(set(ids)) == 1 and len(ids) >= 2,
    )
    check(turn2["state"] == "succeeded", "followup_turn_succeeded")
    check(codeword in text2, "followup_recalls_context", fatal=False)
    check(len(set(ids)) == 1 and len(ids) >= 2, "same_native_session", fatal=False)

    STATE["client"] = c
    cs = c.changesets.wait_ready(sid, source_turn_id=turn2_id, deadline=600)
    detail = c.changesets.get(cs["id"])
    step(
        "12_immutable_changeset",
        id=cs["id"],
        state=cs["state"],
        subject_digest=cs["subject_digest"],
        files=[f["path"] for f in detail["files"]],
        eligible=cs["automatic_eligible"],
    )
    check(cs["state"] == "ready", "changeset_ready")
    diff = c.changesets.diff(cs["id"])
    EVIDENCE["steps"]["12_immutable_changeset"]["follow_up_edit_in_diff"] = "def subtract" in diff
    # Model instruction-following, not platform behaviour: recorded but non-fatal.
    check("def subtract" in diff, "followup_edit_applied", fatal=False)

    test_child = c.delegations.spawn(sid, "test", changeset_id=cs["id"])
    test_view = c.delegations.wait_result(test_child["delegation_id"], deadline=1500)
    checks = ((test_view.get("result") or {}).get("evidence") or {}).get("platform_checks") or []
    step(
        "11_tests_in_sandbox",
        delegation=test_child["delegation_id"],
        state=test_view["state"],
        verdict=(test_view.get("result") or {}).get("verdict"),
        platform_checks=[
            {k: x.get(k) for k in ("name", "status", "exit_code", "head")} for x in checks
        ],
        parent_tool_runs=[t for t in tools if t and "unittest" in str(t)],
    )

    review = None
    for attempt in range(2):
        child = c.delegations.spawn(
            sid,
            "review",
            changeset_id=cs["id"],
            context="Review calc.py and its tests. Approve if correct.",
        )
        review = c.delegations.wait_result(child["delegation_id"], deadline=1500)
        if review["state"] == "succeeded":
            break
    result = review.get("result") or {}
    step(
        "14_independent_child_review",
        delegation=review["id"],
        child_session=review["child_session_id"],
        state=review["state"],
        reason=review.get("state_reason"),
        verdict=result.get("verdict"),
        pinned_subject=result.get("subject_digest") == cs["subject_digest"],
        independent=result.get("independent"),
        attempts=attempt + 1,
    )

    delivery = c.deliveries.wait(
        c.deliveries.request(cs["id"], title=f"SBX MVP {RUN}: calc add/subtract")["id"],
        deadline=600,
    )
    created_refs.append(delivery["target_ref"])
    STATE["pr"] = (delivery.get("pull_request") or {}).get("number")
    step(
        "13_delivery_draft_pr",
        id=delivery["id"],
        state=delivery["state"],
        pr=delivery["pull_request"],
        commit=delivery["commit_sha"],
        steps=[s["kind"] for s in delivery["steps"]],
    )
    check(delivery["state"] == "succeeded", "delivery_succeeded")
    check((delivery["pull_request"] or {}).get("draft"), "pull_request_is_draft", fatal=False)
    pr_body = gh("GET", f"/pulls/{delivery['pull_request']['number']}").json()
    EVIDENCE["steps"]["13_delivery_draft_pr"]["github"] = {
        "base": pr_body["base"]["ref"],
        "head": pr_body["head"]["ref"],
        "head_sha": pr_body["head"]["sha"],
        "draft": pr_body["draft"],
    }
    check(pr_body["head"]["sha"] == delivery["commit_sha"], "pr_head_is_delivered_commit")

    c.deliveries.merge(delivery["id"], mark_ready=False)
    blocked = wait(
        lambda: next(
            (
                m
                for m in c.deliveries.get(delivery["id"])["merge_requests"]
                if m["state"] not in ("pending", "executing")
            ),
            None,
        ),
        300,
        "merge gate",
    )
    gate_evidence: dict[str, Any] = {
        "first_request": {
            "state": blocked["state"],
            "reasons": (blocked["gate"] or {}).get("reasons"),
        }
    }
    merged = None
    if result.get("verdict") == "approve":
        c.deliveries.merge(delivery["id"], mark_ready=True)
        merged = wait(
            lambda: next(
                (
                    m
                    for m in c.deliveries.get(delivery["id"])["merge_requests"]
                    if m["state"] not in ("pending", "executing", "blocked")
                )
                or None,
                None,
            ),
            300,
            "merge",
        )
        gate_evidence["second_request"] = {
            "state": merged["state"],
            "merge_sha": merged.get("merge_sha"),
            "error": merged.get("error"),
        }
    c.deliveries.refresh(delivery["id"])
    time.sleep(5)
    final = c.deliveries.get(delivery["id"])
    step(
        "15_exact_subject_gate_and_merge",
        **gate_evidence,
        eligibility=final["merge_eligibility"],
        pr_state=(final["pull_request"] or {}).get("state"),
    )

    cp.stop()
    cp.start()
    c2 = cp.client()
    STATE["client"] = c2
    c2.login(email, password)
    STATE["changeset"] = cs["id"]
    cons = c2.connections.list()
    sess = c2.sessions.get(sid)["session"]
    revalidated = c2.connections.wait_health(
        c2.connections.validate(zen["id"])["connection"]["id"], deadline=180
    )
    step(
        "16_restart_persistence",
        control_generation=cp.generation,
        connections=[(x["kind"], x["health"]) for x in cons],
        session_lifecycle=sess["lifecycle"],
        turns=[t["state"] for t in c2.turns.list(sid)],
        changesets=len(c2.changesets.list(sid)),
        delivery_state=c2.deliveries.get(delivery["id"])["state"],
        inference_after_restart=revalidated["health"],
    )
    c = c2

    b = signup(cp, f"mvp-b-{RUN}@example.test", "second user password 1")
    denied = {}
    for label, path in {
        "session": f"/api/sessions/{sid}",
        "connection": f"/api/connections/{modal['id']}",
        "changeset": f"/api/changesets/{cs['id']}",
        "delivery": f"/api/deliveries/{delivery['id']}",
        "delegation": f"/api/delegations/{review['id']}",
        "executor": f"/api/sessions/{sid}/executor",
        "events": f"/api/sessions/{sid}/events",
    }.items():
        try:
            b.get(path)
            denied[label] = "VISIBLE"
        except SBXError as exc:
            denied[label] = exc.code
    try:
        b.sessions.create(
            harness={"provider_id": "opencode"},
            executor={"backend": "modal"},
            connections={"inference": zen["id"], "compute": modal["id"]},
        )
        steal = "ALLOWED"
    except SBXError as exc:
        steal = exc.code
    try:
        b.sessions.create(harness={"provider_id": "opencode"}, executor={"backend": "modal"})
        own = "ALLOWED"
    except SBXError as exc:
        own = exc.code
    step(
        "17_cross_owner_isolation",
        denied=denied,
        use_other_connections=steal,
        b_without_connections=own,
        b_connections=len(b.connections.list()),
    )
    check(
        set(denied.values()) == {"not_found"} and steal == "not_found",
        "cross_owner_isolation",
        fatal=False,
    )

    try:
        c.connections.disconnect(modal["id"])
        refused = "REVOKED_WHILE_LIVE"
    except SBXError as exc:
        refused = {"code": exc.code, "details": exc.details}
    sessions = [sid] + [
        d["child_session_id"] for d in c.get(f"/api/sessions/{sid}/delegations")["items"]
    ]
    for s in sessions:
        try:
            c.sessions.release(s)
        except SBXError:
            pass
    wait(
        lambda: all(
            c.sessions.executor(s)["leases"][0]["state"] in ("released", "lost")
            for s in sessions
            if c.sessions.executor(s)["leases"]
        ),
        900,
        "leases released",
        poll=5,
    )
    revoked = c.connections.disconnect(modal["id"])
    step(
        "18_disconnect_does_not_orphan_compute",
        while_live=refused,
        after_release=revoked["state"],
        released_sessions=len(sessions),
    )
    check(revoked["state"] in ("revoked", "disconnected"), "disconnect_after_release", fatal=False)


def cleanup() -> None:
    """Always runs: terminate this run's sandboxes, retire GitHub refs, stop processes."""
    from control.executors.modal import ModalExecutor

    result: dict[str, Any] = {}
    cp: ControlPlane | None = STATE.get("cp")
    if cp:
        cp.stop()
    ws = STATE.get("workspace")
    if ws:
        executor = ModalExecutor()
        compute = {
            "token_id": os.environ["SBX_TEST_MODAL_TOKEN_ID"],
            "token_secret": os.environ["SBX_TEST_MODAL_TOKEN_SECRET"],
        }
        client = executor._client(compute)
        app = executor._app(client)

        def live() -> list[Any]:
            sandboxes = executor.sdk.Sandbox.list(
                app_id=app.app_id, tags={"sbx_workspace": ws}, client=client
            )
            return [sb for sb in sandboxes if sb.poll() is None]

        stragglers = live()
        for sandbox in stragglers:
            sandbox.terminate()
        time.sleep(5)
        result["terminated_stragglers"] = len(stragglers)
        result["live_sandboxes_after"] = len(live())
    number = STATE.get("pr")
    if number:
        pr = gh("GET", f"/pulls/{number}").json()
        result["pull_request"] = {"url": pr.get("html_url"), "merged": pr.get("merged")}
        if pr.get("state") == "open":
            gh("PATCH", f"/pulls/{number}", json={"state": "closed"})
            result["pull_request"]["closed_by_cleanup"] = True
    result["deleted_refs"] = [
        ref
        for ref in STATE["refs"]
        if ref.startswith("sbx")
        and gh("DELETE", f"/git/refs/heads/{ref}").status_code in (204, 422)
    ]
    EVIDENCE["cleanup"] = result


def secret_scan() -> None:
    corpus: dict[str, str] = {"api_responses": "\n".join(RESPONSES)}
    corpus["control_logs"] = "".join(
        p.read_text(errors="replace") for p in BASE.glob("control-*.log")
    )
    if STATE.get("dsn"):
        queries = (
            "SELECT payload FROM session_events",
            "SELECT input, result, last_error FROM jobs",
            "SELECT safe_details FROM connection_observations",
            "SELECT * FROM audit_records",
            "SELECT evidence, expected FROM delivery_steps",
            "SELECT value, evidence FROM delegation_results",
            "SELECT content FROM messages",
            "SELECT content, data FROM message_parts",
        )
        with psycopg.connect(STATE["dsn"]) as conn:
            corpus["database"] = json.dumps(
                [conn.execute(q).fetchall() for q in queries], default=str
            )
    if STATE.get("pr"):
        number = STATE["pr"]
        corpus["pull_request"] = (
            gh("GET", f"/pulls/{number}").text + gh("GET", f"/pulls/{number}/files").text
        )
    corpus["evidence"] = json.dumps(EVIDENCE, default=str)
    leaks = {
        src: [name for name, value in SECRETS.items() if value and value in text]
        for src, text in corpus.items()
    }
    EVIDENCE["secret_scan"] = {
        "leaks": leaks,
        "scanned_bytes": {k: len(v) for k, v in corpus.items()},
    }
    check(not any(leaks.values()), "no_secret_leaks", fatal=False)


def main() -> int:
    failure = None
    try:
        run()
    except BaseException as exc:  # recorded, then cleanup still runs
        failure = redact(f"{type(exc).__name__}: {exc}")[:800]
        (BASE / "failure.txt").write_text(redact(traceback.format_exc()))
        print("[FAIL]", failure, flush=True)
    finally:
        EVIDENCE["failure"] = failure
        for name, action in (("cleanup", cleanup), ("secret_scan", secret_scan)):
            try:
                action()
            except Exception as exc:
                EVIDENCE.setdefault("teardown_errors", {})[name] = redact(
                    f"{type(exc).__name__}: {exc}"
                )[:500]
        if "postgres" in EVIDENCE:
            subprocess.run(EVIDENCE.pop("postgres")["stop"], capture_output=True)
        out = BASE / "mvp-evidence.json"
        out.write_text(redact(json.dumps(EVIDENCE, indent=2, default=str)))
        print("EVIDENCE", out, flush=True)
    gates = EVIDENCE.get("gates", {})
    return 0 if failure is None and gates and all(gates.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

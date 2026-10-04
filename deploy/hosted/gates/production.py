"""Real browser registration and complete production workflow. Output is gate status only."""

import argparse
import json
import os
import secrets
import shlex
import time
import traceback
from pathlib import Path

import httpx
from control.connections import SecretVault
from control.github_app import GitHubAppConfig
from control.real_codex import NativeRPC, native_credentials
from control.real_github import SafeAppClient
from deploy.hosted.gates.codex import wait_run
from deploy.hosted.gates.email import received_code
from deploy.hosted.gates.github import check, wait
from deploy.hosted.rollout import ssh

API = "https://api.sbx-agent.com"
WEB = "https://sbx-agent.com"
STATE = Path("/home/zheng/.config/sbx/production-gate-user.json")
PRODUCT = Path("/home/zheng/.config/sbx/codex-test-user")


def save(state):
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(state, stream)
    STATE.chmod(0o600)


def operator(action, owner, payload=None):
    command = (
        "sudo -u sbx /opt/sbx-browser/.venv/bin/python -m deploy.hosted.operator "
        + action
        + " --user-id "
        + shlex.quote(owner)
    )
    if action == "log-check":
        command = "sudo journalctl -u sbx-hosted --no-pager | " + command
    return ssh("cd /opt/sbx-browser && " + command, payload=payload, timeout=180)


def bootstrap(state):
    from playwright.sync_api import sync_playwright

    inbox = json.loads(Path("/home/zheng/.config/sbx/email-gate-inbox.json").read_text())
    with (
        httpx.Client(
            base_url="https://api.resend.com",
            timeout=15,
            trust_env=False,
            headers={"Authorization": "Bearer " + os.environ["RESEND_API_KEY"]},
        ) as inbound,
        sync_playwright() as pw,
    ):
        browser = pw.chromium.launch(
            headless=True,
            # The operator's local DNS proxy cached the pre-rollout name. This
            # uses public DNS with normal certificate validation and the real origin.
            args=["--host-resolver-rules=MAP sbx-agent.com 104.21.94.204"],
        )
        context = browser.new_context(storage_state=state.get("browser_storage"))
        page = context.new_page()
        page.goto(WEB)
        if not state:
            seen = set()
            for thread in inbound.get(f"/inboxes/{inbox['id']}/threads").json().get("data", []):
                messages = (
                    inbound.get(f"/inboxes/{inbox['id']}/threads/{thread['id']}/emails")
                    .json()
                    .get("data", [])
                )
                seen.update(message["id"] for message in messages)
            page.get_by_role("button", name="Create account", exact=True).click()
            page.get_by_label("Email", exact=True).fill(inbox["receiving_address"])
            page.get_by_role("button", name="Send verification code", exact=True).click()
            page.get_by_label("Verification code").wait_for()
            code = received_code(inbound, inbox["id"], seen)
            page.get_by_label("Verification code").fill(code)
            page.get_by_role("button", name="Verify code", exact=True).click()
            page.get_by_role("heading", name="Set your password", exact=True).wait_for()
            password = secrets.token_urlsafe(24)
            page.get_by_label("Password", exact=True).fill(password)
            page.get_by_role("button", name="Set password", exact=True).click()
            page.get_by_role("textbox", name="Session task").wait_for(timeout=60000)
            user = context.request.get(API + "/auth/me").json()["user"]
            state.update(user_id=user["id"], email=inbox["receiving_address"], password=password)
            save(state)
            cookies = context.cookies(API)
            auth_cookie = next(c for c in cookies if c["name"] == "__Host-sbx_session")
            assert (
                auth_cookie["secure"]
                and auth_cookie["httpOnly"]
                and auth_cookie["sameSite"] == "Lax"
            )
            assert auth_cookie["domain"] == "api.sbx-agent.com"
            assert (
                context.request.post(API + "/auth/logout", data={}, headers={"Origin": WEB}).status
                == 204
            )
            page.reload()
            print(
                "PRODUCTION_GATE browser: fresh real inbox OTP, password and secure cookie PASS",
                flush=True,
            )
        page.get_by_label("Email", exact=True).or_(
            page.get_by_role("textbox", name="Session task")
        ).first.wait_for(timeout=60000)
        if page.get_by_label("Email", exact=True).count():
            page.get_by_label("Email", exact=True).fill(state["email"])
            page.get_by_label("Password", exact=True).fill(state["password"])
            with page.expect_response(
                lambda response: response.url.endswith("/auth/login")
            ) as response:
                page.get_by_role("button", name="Sign in", exact=True).click()
            assert response.value.status == 200, "browser_login_refused"
        page.get_by_role("textbox", name="Session task").wait_for(timeout=60000)
        state["browser_storage"] = context.storage_state()
        save(state)
        cookie = next(c for c in context.cookies(API) if c["name"] == "__Host-sbx_session")
        assert cookie["secure"] and cookie["httpOnly"] and cookie["sameSite"] == "Lax"
        assert cookie["domain"] == "api.sbx-agent.com"
        page.goto(WEB + "/integrations")
        page.get_by_role("heading", name="Integrations", exact=True).wait_for()
        if not state.get("modal_ready"):
            page.get_by_label("Modal Token ID", exact=True).fill(
                os.environ["SBX_TEST_MODAL_TOKEN_ID"]
            )
            page.get_by_label("Modal Token Secret", exact=True).fill(
                os.environ["SBX_TEST_MODAL_TOKEN_SECRET"]
            )
            page.get_by_role("button", name="Connect Modal", exact=True).click()
            page.get_by_text("Ready", exact=True).wait_for(timeout=300000)
            assert page.get_by_label("Modal Token Secret").input_value() == ""
            state["modal_ready"] = True
            save(state)
        print(
            "PRODUCTION_GATE browser: later password login and real Modal runtime Ready PASS",
            flush=True,
        )
        if not state.get("broker_bound"):
            rpc = NativeRPC(PRODUCT, "codex")
            try:
                assert (
                    rpc.call("account/read", {"refreshToken": False})["account"]["type"]
                    == "chatgpt"
                )
            finally:
                rpc.close()
            credentials = native_credentials(PRODUCT)
            envelope = {
                "cipher": SecretVault.from_env().seal(
                    credentials, context="sbx-bootstrap:" + state["user_id"]
                ),
                "installation_id": os.environ["SBX_GITHUB_APP_INSTALLATION_ID"],
                "repos": [os.environ["SBX_GITHUB_TEST_REPO"]],
            }
            operator("bootstrap", state["user_id"], json.dumps(envelope).encode())
            state["broker_bound"] = True
            save(state)
        page.reload()
        page.get_by_text("Connected", exact=True).last.wait_for(timeout=60000)
        status = context.request.get(API + "/hosted/connections/codex").json()
        assert status["connection"]["state"] == "connected"
        assert status["configured"] and status["mock"] is False
        github = context.request.get(API + "/hosted/connections/github").json()
        assert github["configured"] and github["mock"] is False
        assert github["installations"] and not github["installations"][0]["suspended"]
        storage = page.evaluate("JSON.stringify([localStorage, sessionStorage])")
        assert os.environ["SBX_TEST_MODAL_TOKEN_SECRET"] not in storage
        print(
            "PRODUCTION_GATE browser: real App binding and native Codex connection PASS",
            flush=True,
        )
        if state.get("merged"):
            page.goto(WEB + "/sessions/" + state["author_session"])
            page.get_by_role("button", name="Worked", exact=False).first.click(timeout=60000)
            page.get_by_text("unittest", exact=False).first.wait_for(timeout=60000)
            tabs = page.get_by_role("region", name="Session workspace tools").locator(".pane-tabs")
            tabs.get_by_role("button", name="Changes", exact=False).click()
            page.get_by_text("sbx_production_greeting.py", exact=False).first.wait_for(
                timeout=60000
            )
            tabs.get_by_role("button", name="Review", exact=True).click()
            link = page.locator(
                'a[href="https://github.com/soren-labs/sbx-e2e-test/pull/'
                + str(state["test_pr"])
                + '"]'
            )
            link.first.wait_for(timeout=60000)
            state["browser_delivery_pass"] = True
            save(state)
            print(
                "PRODUCTION_GATE browser: durable native activity, changes and real PR link PASS",
                flush=True,
            )
            page.goto(WEB + "/settings")
            page.get_by_role("heading", name="API Keys", exact=True).wait_for(timeout=60000)
            if not state.get("browser_key_pass"):
                page.get_by_label("Key name", exact=True).fill("Browser production gate")
                page.get_by_role("button", name="Create API key", exact=True).click()
                field = page.get_by_label("New API key", exact=True)
                field.wait_for(timeout=60000)
                state["api_key"] = field.input_value()
                state["browser_key_pass"] = True
                save(state)
                page.get_by_role("button", name="Dismiss key", exact=True).click()
            page.reload()
            page.get_by_text("Browser production gate", exact=True).wait_for(timeout=60000)
            assert page.get_by_label("New API key", exact=True).count() == 0
            with httpx.Client(
                base_url=API,
                timeout=30,
                trust_env=False,
                headers={
                    "Authorization": "Bearer " + state["api_key"],
                },
            ) as personal:
                check(personal.get("/v2/sessions/" + state["api_session"]))
            print(
                "PRODUCTION_GATE browser: personal key, reload concealment and API query PASS",
                flush=True,
            )

        context.close()
        browser.close()


def client_for(state):
    client = httpx.Client(base_url=API, headers={"Origin": WEB}, timeout=120, trust_env=False)
    return client


def live_tests(client, sid, state):
    deadline = time.monotonic() + 300
    cursor, live, tested, captured = 0, False, False, []
    while time.monotonic() < deadline:
        response = client.post(f"/hosted/sessions/{sid}/connect", json={})
        if response.status_code == 409:
            time.sleep(1)
            continue
        grant = check(response)
        with httpx.Client(timeout=75, trust_env=False) as direct:
            with direct.stream(
                "GET",
                grant["url"],
                headers={
                    "Origin": WEB,
                    "Authorization": "Bearer " + grant["grant"],
                    "Last-Event-ID": str(cursor),
                },
            ) as stream:
                assert stream.status_code == 200
                assert "text/event-stream" in stream.headers["content-type"]
                assert stream.headers["access-control-allow-origin"] == WEB
                for line in stream.iter_lines():
                    if line.startswith("id:"):
                        cursor = int(line[3:])
                    if not line.startswith("data:"):
                        continue
                    captured.append(line)
                    event = json.loads(line[5:])
                    if event.get("type", "").startswith("item.") and not live:
                        live = check(client.get(f"/v2/sessions/{sid}"))["session"][
                            "status"
                        ] not in ("finished", "failed")
                    item = event.get("item") or {}
                    tested |= (
                        event.get("type") == "item.completed"
                        and item.get("type") == "command_execution"
                        and "unittest" in item.get("command", "")
                        and item.get("exit_code") == 0
                    )
                    if tested and live:
                        break
            if tested and live:
                break
    assert live and tested, "missing_live_native_unittest"
    events = "\n".join(captured)
    assert all(
        value not in events
        for value in (
            state["password"],
            os.environ["RESEND_API_KEY"],
            os.environ["SBX_TEST_MODAL_TOKEN_SECRET"],
        )
    )
    operator("event-check", state["user_id"], events.encode())
    state["live_output_pass"] = True
    save(state)
    print(
        "PRODUCTION_GATE: direct Modal HTTPS/SSE, live native output and unittest exit 0 PASS",
        flush=True,
    )


def review(client, sid):
    rid = check(client.post(f"/hosted/sessions/{sid}/review-sessions", json={}), 201)["session_id"]
    wait(client, rid, timeout=900)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = check(client.get(f"/hosted/review-sessions/{rid}"))
        if result["status"] == "completed":
            assert result["review"]["independent"]
            return rid, result
        if result["status"] == "failed":
            raise RuntimeError("review_contract_failed")
        time.sleep(2)
    raise RuntimeError("review_record_timeout")


def workflow(state):
    repo = os.environ["SBX_GITHUB_TEST_REPO"]
    assert repo == "soren-labs/sbx-e2e-test"
    with client_for(state) as client:
        check(
            client.post(
                "/auth/login", json={"email": state["email"], "password": state["password"]}
            )
        )
        if not state.get("base_branch"):
            app = SafeAppClient(
                GitHubAppConfig(
                    app_id=os.environ["SBX_GITHUB_APP_ID"],
                    slug=os.environ["SBX_GITHUB_APP_SLUG"],
                    private_key=Path(os.environ["SBX_GITHUB_APP_PRIVATE_KEY_PATH"]).read_text(),
                )
            )
            token, _ = app.create_installation_token(
                int(os.environ["SBX_GITHUB_APP_INSTALLATION_ID"]),
                repositories=[repo.split("/", 1)[1]],
            )
            with httpx.Client(
                base_url="https://api.github.com",
                trust_env=False,
                timeout=30,
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/vnd.github+json",
                },
            ) as github:
                base_sha = check(github.get(f"/repos/{repo}/git/ref/heads/main"))["object"]["sha"]
                branch = "sbx-production/base-" + secrets.token_hex(6)
                check(
                    github.post(
                        f"/repos/{repo}/git/refs",
                        json={"ref": "refs/heads/" + branch, "sha": base_sha},
                    ),
                    201,
                )
            state["base_branch"] = branch
            save(state)
        if not state.get("author_session"):
            result = check(
                client.post(
                    "/v2/sessions",
                    json={
                        "prompt": "Create sbx_production_greeting.py. Its greet() returns hello. "
                        "test_sbx_production_greeting.py containing a standard-library unittest. "
                        "Run the unittest and commit only those two files. Do not push, inspect "
                        "credentials or print environment. Report tests. Keep the code minimal.",
                        "repository": {"repo": repo, "ref": state["base_branch"]},
                        "execution": {"provider": "codex", "model": "gpt-6.1-sol"},
                    },
                ),
                201,
            )
            state["author_session"] = result["session"]["id"]
            save(state)
        sid = state["author_session"]
        if not state.get("live_output_pass"):
            live_tests(client, sid, state)
        wait(client, sid, timeout=900)
        assert check(client.get(f"/v2/sessions/{sid}/changes/diff"))["files"]
        print("PRODUCTION_GATE: real coding, code changes and tests PASS", flush=True)
        if not state.get("delivered"):
            check(
                client.post(f"/v2/sessions/{sid}/deliver", json={"pull_request": {"draft": True}})
            )
            state["delivered"] = True
            save(state)
        if not state.get("review_pass"):
            rid, result = review(client, sid)
            state.setdefault("reviewers", []).append(rid)
            if result["review"]["verdict"] == "request_changes":
                prompt = (
                    "Fix these review findings, run the unittest and commit the fix: "
                    + json.dumps(result["review"]["findings"])
                )
                check(client.post(f"/v2/sessions/{sid}/messages", json={"prompt": prompt}), 202)
                wait_run(client, sid, 2, timeout=900)
                check(
                    client.post(
                        f"/v2/sessions/{sid}/deliver", json={"pull_request": {"draft": False}}
                    )
                )
                rid, result = review(client, sid)
                state["reviewers"].append(rid)
            assert result["review"]["verdict"] == "approve"
            state["review_pass"] = True
            save(state)
        if not state.get("merged"):
            check(
                client.post(f"/v2/sessions/{sid}/deliver", json={"pull_request": {"draft": False}})
            )
            merged = check(client.post(f"/v1/tasks/{sid}/merge", json={}))
            assert merged["revision"]["delivery"]["merged"]
            state["test_pr"] = merged["revision"]["delivery"]["pull_request"]["number"]
            state["merged"] = True
            save(state)
        print(
            "PRODUCTION_GATE: real PR, independent review, pass/fix and merge PASS",
            flush=True,
        )
        if not state.get("api_key"):
            key = check(
                client.post(
                    "/hosted/api-keys", json={"label": "Production gate", "scopes": ["agents"]}
                ),
                201,
            )
            state["api_key"] = key["key"]
            save(state)
        with httpx.Client(
            base_url=API,
            timeout=120,
            trust_env=False,
            headers={"Authorization": "Bearer " + state["api_key"]},
        ) as programmatic:
            if not state.get("api_session"):
                result = check(
                    programmatic.post(
                        "/v2/sessions",
                        json={
                            "prompt": "Inspect sbx_production_greeting.py and run its unittest. "
                            "Do not modify files, inspect credentials, push or print environment.",
                            "repository": {"repo": repo, "ref": state["base_branch"]},
                            "execution": {"provider": "codex", "model": "gpt-6.1-sol"},
                        },
                    ),
                    201,
                )
                state["api_session"] = result["session"]["id"]
                save(state)
            wait(programmatic, state["api_session"], timeout=900)
            check(programmatic.get("/v2/sessions/" + state["api_session"]))
        print(
            "PRODUCTION_GATE: personal API key and API-created real native Session PASS", flush=True
        )
        before = json.loads(operator("evidence", state["user_id"]))
        operator("refresh", state["user_id"])
        refreshed = json.loads(operator("evidence", state["user_id"]))
        assert refreshed["codex"]["credential_version"] > before["codex"]["credential_version"]
        ssh("sudo systemctl restart sbx-hosted", timeout=60)
        for _ in range(60):
            try:
                if httpx.get(API + "/hosted/health", timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        after = json.loads(operator("evidence", state["user_id"]))
        assert after == refreshed
        for name in ("modal", "codex"):
            assert check(client.get("/hosted/connections/" + name))["connection"]["state"] in (
                "connected",
                "ready",
            )
        assert check(client.get("/hosted/connections/github"))["installations"]
        check(client.get("/v2/sessions/" + sid))
        check(client.get("/v2/sessions/" + state["api_session"]))
        with httpx.Client(
            base_url=API,
            timeout=120,
            trust_env=False,
            headers={
                "Authorization": "Bearer " + state["api_key"],
            },
        ) as programmatic:
            check(programmatic.get("/v2/sessions/" + state["api_session"]))
            history = check(programmatic.get("/v2/sessions/" + state["api_session"] + "/history"))
            n = max(row["n"] for row in history["runs"]) + 1
            check(
                programmatic.post(
                    "/v2/sessions/" + state["api_session"] + "/messages",
                    json={
                        "prompt": "Run python -m unittest test_sbx_production_greeting.py again. "
                        "Report the test result. Do not modify files, inspect credentials "
                        "or print environment.",
                    },
                ),
                202,
            )
            # The asynchronous acknowledgement may precede the new ledger row.
            wait_run(programmatic, state["api_session"], n, timeout=900)
            history = check(programmatic.get("/v2/sessions/" + state["api_session"] + "/history"))
            assert any(
                row["event"].get("n") == n
                and row["event"].get("type") == "item.completed"
                and (row["event"].get("item") or {}).get("type") == "command_execution"
                and "unittest" in (row["event"].get("item") or {}).get("command", "")
                and (row["event"].get("item") or {}).get("exit_code") == 0
                for row in history["events"]
            ), "restart_native_unittest_missing"
        operator("log-check", state["user_id"])
        print(
            "PRODUCTION_GATE: VPS rotation, restart, durable integrations and logs PASS",
            flush=True,
        )
        state["complete"] = True
        save(state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["bootstrap", "workflow", "all"], default="all")
    args = parser.parse_args()
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if args.phase in ("bootstrap", "all"):
        bootstrap(state)
    if args.phase in ("workflow", "all"):
        workflow(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        frame = next(
            (frame for frame in reversed(frames) if Path(frame.filename).name == "production.py"),
            frames[-1],
        )
        print(
            f"PRODUCTION_GATE failed: {type(error).__name__} "
            f"at {Path(frame.filename).name}:{frame.lineno}",
            flush=True,
        )
        raise SystemExit(1) from None

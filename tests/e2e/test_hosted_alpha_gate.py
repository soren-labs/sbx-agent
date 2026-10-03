"""All eleven hosted Alpha steps, including a new control-server OS process."""

import base64
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from control.app import create_app
from control.auth_email import MockEmailSender
from control.auth_store import AuthDatabase, AuthStore
from control.connections import SecretVault
from tests.e2e.hosted_fixture import hosted_browser
from tests.e2e.test_hosted_workflow_browser import complete_coding_loop
from tests.integration.control.test_auth_postgres import postgres  # noqa: F401


@pytest.mark.parametrize("storage", ["sqlite", "postgres"])
def test_hosted_alpha_eleven_step_gate(storage, request, tmp_path, monkeypatch):
    url = request.getfixturevalue("postgres")[0] if storage == "postgres" else None
    monkeypatch.setenv("SBX_CONNECTIONS_MODE", "mock")
    monkeypatch.setenv("CODEX_BIN", str(Path("tests/fakes/hosted_codex.py").resolve()))
    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    clock = [time.time()]
    key = secrets.token_bytes(32)
    sender = MockEmailSender()
    database_path = tmp_path / "alpha.sqlite3"
    auth = AuthStore(
        AuthDatabase(database_url=url) if url else AuthDatabase(path=database_path),
        clock=lambda: clock[0],
    )
    app = create_app(
        auth_store=auth,
        email_sender=sender,
        hosted=True,
        state_backend="postgres",
        connection_vault=SecretVault(key),
        runner_cmd=[sys.executable, "-m", "runtime.runner"],
    )
    email = f"alpha-{secrets.token_hex(4)}@example.test"
    password = secrets.token_urlsafe(24)
    direct_events = []
    with hosted_browser(app) as (page, base, playwright):
        page.on(
            "response",
            lambda response: (
                direct_events.append(response.url)
                if "/sessions/" in response.url
                and response.url.endswith("/events")
                and not response.url.startswith(base)
                else None
            ),
        )
        page.goto(base)
        page.get_by_role("button", name="Create account", exact=True).click()
        page.get_by_role("textbox", name="Email", exact=True).fill(email)
        page.get_by_role("button", name="Send verification code").click()
        playwright.expect(page.get_by_role("textbox", name="Verification code")).to_be_visible()
        page.get_by_role("textbox", name="Verification code").fill(sender.latest_code(email))
        page.get_by_role("button", name="Verify code").click()
        playwright.expect(page.get_by_label("Password", exact=True)).to_be_visible()
        page.get_by_label("Password", exact=True).fill(password)
        page.get_by_role("button", name="Set password", exact=True).click()
        playwright.expect(page.get_by_role("textbox", name="Session task")).to_be_visible()
        page.goto(base + "/settings")
        page.get_by_role("button", name="Sign out", exact=True).click()
        playwright.expect(page.get_by_role("button", name="Sign in", exact=True)).to_be_visible()
        page.get_by_role("textbox", name="Email", exact=True).fill(email)
        page.get_by_label("Password", exact=True).fill(password)
        page.get_by_role("button", name="Sign in", exact=True).click()
        playwright.expect(page.get_by_role("textbox", name="Session task")).to_be_visible()
        page.goto(base + "/integrations")
        page.get_by_role("button", name="Connect with Modal authorization").click()
        playwright.expect(page.get_by_text("Ready", exact=True)).to_be_visible()
        page.get_by_role("button", name="Connect GitHub", exact=True).click()
        playwright.expect(page.get_by_text("Connected:", exact=False)).to_be_visible()
        page.get_by_role("button", name="Connect Codex", exact=True).click()
        playwright.expect(
            page.get_by_role("region", name="Codex connection").get_by_text("Connected", exact=True)
        ).to_be_visible()
        user = auth.find_user_by_email(email)
        assert user is not None
        prior = app.state.connections.get(user.id, "codex").metadata["credential_version"]
        clock[0] += 270
        page.get_by_role("button", name="Check Codex connection").click()
        playwright.expect(page.get_by_role("button", name="Check Codex connection")).to_be_enabled()
        assert (
            app.state.connections.get(user.id, "codex").metadata["credential_version"] == prior + 1
        )
        assert app.state.codex_broker.provider.calls_by_owner[user.id] == 1
        repo = app.state.github_connections.for_user(user.id)._client.repositories[0]
        author = complete_coding_loop(page, base, playwright, repo)
        assert direct_events
        # Release retained reviewer compute before the next Session; durable
        # review/history rows remain. This uses the existing lifecycle API.
        for identifier, _ in app.state.database_records.rows(
            "hosted_review_sessions", owner=user.id
        ):
            reviewer = app.state.task_store.get(identifier)
            assert (
                page.request.delete(base + f"/v1/agents/{reviewer.agent_id}", data={}).status == 200
            )
        page.goto(base + "/settings")
        page.get_by_role("textbox", name="Key name").fill("Alpha gate")
        page.get_by_role("button", name="Create API key", exact=True).click()
        playwright.expect(page.get_by_role("textbox", name="New API key")).to_be_visible()
        token = page.get_by_role("textbox", name="New API key").input_value()
        assert token.startswith("sbx_")
        page.get_by_role("button", name="Dismiss key").click()
        page.reload()
        playwright.expect(page.get_by_text("Alpha gate", exact=True)).to_be_visible()
        assert page.get_by_role("textbox", name="New API key").count() == 0
        assert token not in page.evaluate("JSON.stringify({...localStorage, ...sessionStorage})")
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(base_url=base, headers=headers, timeout=15) as api:
            created = api.post(
                "/v2/sessions",
                json={
                    "prompt": "Create another greeting",
                    "repository": {"repo": repo, "ref": "main"},
                    "execution": {"provider": "codex"},
                },
            )
            assert created.status_code == 201
            api_session = created.json()["session"]["id"]
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                detail = api.get(f"/v2/sessions/{api_session}").json()
                if detail["session"]["status"] in {"finished", "failed"}:
                    break
                time.sleep(0.05)
            assert detail["session"]["status"] == "finished"
            history = api.get(f"/v2/sessions/{api_session}/history")
            assert history.status_code == 200
            assert api.get("/v2/sessions").json()["total"] >= 4
            listing = page.request.get(base + "/hosted/api-keys").json()
            assert token not in str(listing)
    # The previous server is stopped. A separate interpreter reconstructs
    # every service/store; no parent app, backend registry or cache is shared.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    env = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "LANG", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME")
        if name in os.environ
    }
    env.update(
        SBX_CONNECTIONS_MODE="mock",
        SBX_AUTH_DB_PATH=str(database_path),
        SBX_CONNECTION_ENCRYPTION_KEY=base64.urlsafe_b64encode(key).decode(),
        SBX_RESTART_SOCKET_FD=str(listener.fileno()),
        PYTHONPATH=str(Path.cwd()),
    )
    if url:
        env["DATABASE_URL"] = url
    child = subprocess.Popen(
        [sys.executable, str(Path("tests/e2e/restart_hosted.py").resolve())],
        env=env,
        pass_fds=[listener.fileno()],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    listener.close()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=3) as api:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if api.get("/hosted/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert child.poll() is None, "reconstructed control server exited"
                time.sleep(0.05)
            else:
                pytest.fail("reconstructed control server did not become ready")
            assert api.get(f"/v2/sessions/{api_session}").json()["session"]["status"] == "finished"
            assert api.get(f"/v2/sessions/{api_session}/history").json() == history.json()
            assert (
                api.get(f"/v2/sessions/{author}").json()["session"]["delivery"]["pull_request"][
                    "state"
                ]
                == "merged"
            )
            assert len(api.get(f"/v1/tasks/{author}/reviews").json()["reviews"]) == 2
            assert api.get("/hosted/connections/modal").json()["connection"]["state"] == "ready"
            assert (
                api.get("/hosted/connections/codex").json()["connection"]["metadata"][
                    "credential_version"
                ]
                == prior + 1
            )
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

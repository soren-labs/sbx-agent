"""Opt-in real email gate; consumes inbound mail, never the sender's OTP payload.

Run with the secure integration env loaded. Inbox metadata is outside the repo.
All output is bounded gate status; codes, passwords and provider bodies stay in memory.
"""

import json
import os
import re
import secrets
import tempfile
import time
import traceback
from pathlib import Path

import httpx
from control.resend_email import ResendEmailSender


def received_code(client, inbox_id, excluded):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        threads = client.get(f"/inboxes/{inbox_id}/threads").json().get("data", [])
        for thread in threads:
            messages = (
                client.get(f"/inboxes/{inbox_id}/threads/{thread['id']}/emails")
                .json()
                .get("data", [])
            )
            for message in reversed(messages):
                if message.get("direction") != "inbound" or message["id"] in excluded:
                    continue
                if message.get("subject") != "Verify your SBX Agent email":
                    continue
                match = re.search(r"code is ([0-9]{6})", message.get("text") or "")
                if match:
                    excluded.add(message["id"])
                    return match[1]
        time.sleep(3)
    raise RuntimeError("real_inbox_delivery_timeout")


def main():
    sender = ResendEmailSender.from_env()
    assert sender.preflight()["ready"]
    inbox = json.loads(Path(os.environ["SBX_REAL_INBOX_FILE"]).read_text())
    email = inbox["receiving_address"]
    key = os.environ["RESEND_API_KEY"]
    # This isolated gate process must not initialize ambient production state.
    for name in list(os.environ):
        if name.startswith(("SBX_", "MODAL_", "AWS_", "CODEX_")) or name == "DATABASE_URL":
            os.environ.pop(name)
    with tempfile.TemporaryDirectory(prefix="sbx-email-real-") as directory:
        os.environ["HOME"] = directory
        os.environ["XDG_CONFIG_HOME"] = directory
        os.environ["SBX_BACKEND"] = "local"
        from control.app import create_app
        from control.auth_store import AuthDatabase, AuthStore
        from fastapi.testclient import TestClient

        store = AuthStore(AuthDatabase(path=Path(directory) / "gate.sqlite3"))
        app = create_app(auth_store=store, email_sender=sender)
        with (
            TestClient(app, base_url="https://testserver") as browser,
            httpx.Client(
                base_url="https://api.resend.com",
                timeout=15,
                headers={"Authorization": f"Bearer {key}"},
                trust_env=False,
            ) as inbound,
        ):
            seen = set()
            for thread in inbound.get(f"/inboxes/{inbox['id']}/threads").json().get("data", []):
                messages = (
                    inbound.get(f"/inboxes/{inbox['id']}/threads/{thread['id']}/emails")
                    .json()
                    .get("data", [])
                )
                seen.update(message["id"] for message in messages)
            response = browser.post("/auth/register", json={"email": email})
            assert response.status_code == 202
            first_challenge = response.json()["challenge_id"]
            old_code = received_code(inbound, inbox["id"], seen)
            print("REAL_GATE email: inbound OTP delivered from verified domain", flush=True)
            response = browser.post("/auth/register", json={"email": email})
            print("REAL_GATE email: cooldown response HTTP", response.status_code, flush=True)
            assert response.status_code == 429
            time.sleep(int(response.headers["Retry-After"]) + 1)
            response = browser.post("/auth/register", json={"email": email})
            print("REAL_GATE email: resend response HTTP", response.status_code, flush=True)
            assert response.status_code == 202
            challenge = response.json()["challenge_id"]
            code = received_code(inbound, inbox["id"], seen)
            assert (
                browser.post(
                    "/auth/verify",
                    json={
                        "challenge_id": first_challenge,
                        "code": old_code,
                    },
                ).status_code
                == 400
            )
            response = browser.post("/auth/verify", json={"challenge_id": challenge, "code": code})
            print("REAL_GATE email: verification response HTTP", response.status_code, flush=True)
            assert response.status_code == 200
            password = secrets.token_urlsafe(24)
            assert (
                browser.post(
                    "/auth/password",
                    json={
                        "registration_token": response.json()["registration_token"],
                        "password": password,
                    },
                ).status_code
                == 201
            )
            assert browser.post("/auth/logout", json={}).status_code == 204
            assert (
                browser.post(
                    "/auth/login",
                    json={
                        "email": email,
                        "password": password,
                    },
                ).status_code
                == 200
            )
            print(
                "REAL_GATE email: cooldown, resend, OTP, password and later login PASS", flush=True
            )
            app.state.hosted_auth.sender = ResendEmailSender("REDACTED", sender.sender)
            failed_email = "failure-" + secrets.token_hex(4) + "@sbx-agent.com"
            response = browser.post("/auth/register", json={"email": failed_email})
            assert response.status_code == 503
            assert response.json() == {"error": "auth_unavailable"}
            print("REAL_GATE email: real provider rejection safely returns 503 PASS", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("REAL_GATE email: FAIL " + type(error).__name__, flush=True)
        print("Failure line:", traceback.extract_tb(error.__traceback__)[-1].lineno, flush=True)
        raise SystemExit(1) from None

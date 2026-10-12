"""One-time, idempotent setup of the two stable staging test users.

Creates the users if they do not exist, verifies their email and stores their
Connections in the staging Vault. Run from an operator machine; provider
credentials are read from the environment, sent only to the staging API and
never printed. GitHub Actions never needs these provider credentials: it signs
in as the two users.

Environment:
  SBX_STAGING_URL, SBX_STAGING_USER_{A,B}_EMAIL, SBX_STAGING_USER_{A,B}_PASSWORD
  SBX_STAGING_MAIL_COMMAND   shell command printing stored mail JSON, one per line
                             (``{email}`` is replaced by the address being verified)
  SBX_TEST_MODAL_TOKEN_ID / SBX_TEST_MODAL_TOKEN_SECRET          (both users)
  SBX_TEST_INFERENCE_API_KEY [+ _ENDPOINTS, _MODEL]              (both users)
  SBX_TEST_GITHUB_TOKEN                                          (user A only)
Missing provider credentials are reported as pending, not as success.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from tests.e2e_modal.inference import inference_credential
from tests.e2e_staging.common import Account, SBXClient, SBXError, account, client, staging_url

DEFAULT_MAIL_COMMAND = (
    "ssh sbx-staging 'sudo docker compose -p sbx-staging exec -T sbx "
    'sh -c "for f in /var/lib/sbx/mail/*.json; do cat \\"\\$f\\"; echo; done"\''
)


def verification_token(email: str) -> str:
    command = os.environ.get("SBX_STAGING_MAIL_COMMAND", DEFAULT_MAIL_COMMAND)
    command = command.replace("{email}", email)
    out = subprocess.run(command, shell=True, check=True, capture_output=True, text=True).stdout
    tokens = [
        mail["body"].split("token=")[1].strip()
        for mail in (json.loads(line) for line in out.splitlines() if line.strip())
        if mail["to"] == email.lower() and "token=" in mail["body"]
    ]
    if not tokens:
        raise SystemExit(f"no verification mail found for {email}")
    return tokens[-1]


def ensure_user(url: str, who: Account) -> SBXClient:
    c = client(url)
    try:
        c.login(who.email, who.password)
        print(f"[ok] user {who.name}: exists")
        return c
    except SBXError as exc:
        if exc.status not in (401, 403):
            raise
    c.register(who.email, who.password)
    c.verify_email(verification_token(who.email))
    c.login(who.email, who.password)
    print(f"[ok] user {who.name}: created and verified")
    return c


def ensure_connection(
    c: SBXClient, who: Account, kind: str, credential: dict[str, Any] | None
) -> str:
    existing = [x for x in c.connections.list() if x["kind"] == kind and x["state"] == "configured"]
    if existing:
        health = existing[0]["health"]
        print(f"[ok] user {who.name}: {kind} connection exists (health={health})")
        return "ready" if health == "ready" else "unhealthy"
    if credential is None:
        print(f"[pending] user {who.name}: no credential provided for {kind}")
        return "pending"
    created = c.connections.add(kind, credential, f"staging-e2e {kind}")
    health = c.connections.wait_health(created["id"], deadline=180)["health"]
    print(
        f"[{'ok' if health == 'ready' else 'FAIL'}] user {who.name}: {kind} added (health={health})"
    )
    return "ready" if health == "ready" else "unhealthy"


def main() -> int:
    url = staging_url()
    env = os.environ
    modal = (
        {
            "token_id": env["SBX_TEST_MODAL_TOKEN_ID"],
            "token_secret": env["SBX_TEST_MODAL_TOKEN_SECRET"],
        }
        if env.get("SBX_TEST_MODAL_TOKEN_ID") and env.get("SBX_TEST_MODAL_TOKEN_SECRET")
        else None
    )
    inference = inference_credential() if env.get("SBX_TEST_INFERENCE_API_KEY") else None
    github = {"token": env["SBX_TEST_GITHUB_TOKEN"]} if env.get("SBX_TEST_GITHUB_TOKEN") else None
    results: list[str] = []
    for name in ("a", "b"):
        who = account(name)
        c = ensure_user(url, who)
        results.append(ensure_connection(c, who, "modal", modal))
        results.append(ensure_connection(c, who, "inference_api", inference))
        if name == "a":
            results.append(ensure_connection(c, who, "github", github))
    if "unhealthy" in results:
        return 1
    if "pending" in results:
        print("bootstrap incomplete: some provider credentials are pending")
        return 2
    print("bootstrap complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())

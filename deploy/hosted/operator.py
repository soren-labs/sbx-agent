"""Trusted VPS operations. No browser credential import and no raw auth-file copying."""

import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path

from control.auth_store import AuthDatabase, AuthStore
from control.codex_broker import CodexBroker
from control.connections import ConnectionStore, SecretVault
from control.real_codex import NativeCodexProvider, NativeRPC, write_native_auth
from control.real_github import GitHubFactory


def service_store():
    path = Path("/etc/sbx-hosted.env")
    if path.stat().st_mode & 0o077:
        raise ValueError("service_environment_must_be_private")
    for line in path.read_text().splitlines():
        name, separator, value = line.partition("=")
        if separator and not name.startswith("#"):
            os.environ[name] = json.loads(value)
    return ConnectionStore(AuthStore(AuthDatabase.from_env()), SecretVault.from_env())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["bootstrap", "refresh", "evidence", "log-check", "event-check"]
    )
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()
    store = service_store()
    provider = NativeCodexProvider()
    broker = CodexBroker(store, provider)
    if args.action == "bootstrap":
        envelope = json.load(sys.stdin)
        credentials = store.vault.open(envelope["cipher"], context="sbx-bootstrap:" + args.user_id)
        with provider.private_home() as home:
            write_native_auth(home, credentials)
            rpc = NativeRPC(home, provider.binary)
            try:
                account = rpc.call("account/read", {"refreshToken": False}).get("account")
                if not account or account.get("type") != "chatgpt":
                    raise ValueError("native_account_unavailable")
            finally:
                rpc.close()
        GitHubFactory().bind_installation(
            store, args.user_id, int(envelope["installation_id"]), envelope["repos"]
        )
        store.connect(
            args.user_id,
            "codex",
            credentials,
            metadata_factory=lambda version: {
                "credential_version": version,
                "expires_at": credentials["expires_at"],
                "models": credentials["models"],
                "authorization_mechanism": "official_codex_app_server",
            },
        )
        print("Trusted App binding and encrypted native grant import PASS")
    elif args.action == "refresh":
        record = store.get(args.user_id, "codex")
        credentials = store.credentials(record)
        credentials["expires_at"] = store.auth.clock() + 30
        record.credential_cipher = store.vault.seal(credentials, context=record.context)
        record.metadata["expires_at"] = credentials["expires_at"]
        store.save(record)
        lease = broker.lease(args.user_id)
        print(
            json.dumps({"native_refresh": "PASS", "credential_version": lease.credential_version})
        )
    elif args.action in ("log-check", "event-check"):
        logs = sys.stdin.buffer.read()
        if not logs:
            raise ValueError("production_journal_missing")
        credentials = store.credentials(store.get(args.user_id, "codex"))
        values = [credentials[name] for name in ("access_token", "refresh_token", "id_token")]
        values += [
            os.environ["RESEND_API_KEY"],
            Path(os.environ["SBX_GITHUB_APP_PRIVATE_KEY_PATH"]).read_text(),
        ]
        modal = store.credentials(store.get(args.user_id, "modal"))
        values += [modal["token_id"], modal["token_secret"]]
        assert all(value.encode() not in logs for value in values)
        assert not re.search(
            rb"ghs_[a-zA-Z0-9]{30,}|eyJ[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]{20,}",
            logs,
        )
        print("Production credential leak scan PASS: " + args.action)
    else:
        output = {}
        for name in ("modal", "github", "codex"):
            record = store.get(args.user_id, name)
            output[name] = {"state": record.state, "version": record.version}
            if name == "codex":
                output[name]["credential_version"] = record.metadata["credential_version"]
        print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        print(
            "Trusted operator action failed: "
            + type(error).__name__
            + " at "
            + Path(frame.filename).name
            + ":"
            + str(frame.lineno)
        )
        raise SystemExit(1) from None

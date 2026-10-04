"""Real native Codex rotation and three concurrent user-owned Modal Sessions."""

import json
import os
import secrets
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore, PersistentApiKeyStore
from control.codex_broker import CodexBroker, ProviderUnauthorized
from control.connections import ConnectionStore, SecretVault
from control.hosted_auth import HostedAuthError
from control.real_codex import NativeCodexProvider
from control.real_github import GitHubFactory
from control.real_modal import RealModalProvider
from control.sandbox_io import read_text
from deploy.hosted.gates.common import isolated_postgres
from deploy.hosted.gates.github import check, wait
from fastapi.testclient import TestClient

PRODUCT_HOME = "/home/zheng/.config/sbx/codex-test-user"


def force_due(store, owner):
    record = store.get(owner, "codex")
    credentials = store.credentials(record)
    credentials["expires_at"] = store.auth.clock() + 30
    record.credential_cipher = store.vault.seal(credentials, context=record.context)
    record.metadata["expires_at"] = credentials["expires_at"]
    store.save(record)


def wait_run(client, identifier, n, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        history = check(client.get(f"/v2/sessions/{identifier}/history"))
        run = next((row for row in history["runs"] if row["n"] == n), None)
        if run and run["status"] in {"finished", "failed", "cancelled"}:
            assert run["status"] == "finished", "continuation_failed"
            return run
        time.sleep(1)
    raise RuntimeError("continuation_timeout")


def main():
    factory = GitHubFactory()
    compute = RealModalProvider()
    provider = NativeCodexProvider(bootstrap_home=PRODUCT_HOME)
    os.environ["SBX_CONNECTIONS_MODE"] = "production"
    os.environ["SBX_BROWSER_ORIGINS"] = "https://sbx-agent.com"
    with isolated_postgres() as url:
        auth = AuthStore(AuthDatabase(database_url=url))
        vault = SecretVault(secrets.token_bytes(32))
        app = create_app(
            auth_store=auth,
            hosted=True,
            state_backend="postgres",
            connection_vault=vault,
            modal_provider=compute,
            compute_provider=compute,
            github_factory=factory,
            codex_provider=provider,
            runner_cmd=["python", "-m", "runtime.runner"],
            default_model="gpt-6.1-sol",
            max_concurrent=5,
        )
        user = auth.create_user()
        store, broker = app.state.connections, app.state.codex_broker
        provider.import_login(user.id, PRODUCT_HOME)
        original = store.credentials(store.get(user.id, "codex"))
        store.connect(
            user.id,
            "modal",
            {
                "token_id": os.environ["SBX_TEST_MODAL_TOKEN_ID"],
                "token_secret": os.environ["SBX_TEST_MODAL_TOKEN_SECRET"],
            },
        )
        app.state.modal_connections.provision(user.id)
        repo = os.environ["SBX_GITHUB_TEST_REPO"]
        assert repo == "soren-labs/sbx-e2e-test"
        factory.bind_installation(
            store, user.id, int(os.environ["SBX_GITHUB_APP_INSTALLATION_ID"]), [repo]
        )
        # Safely cross a broker expiry boundary using the official native refresh.
        # The dedicated native cache stays authoritative for this disposable gate.
        force_due(store, user.id)
        with ThreadPoolExecutor(max_workers=3) as pool:
            leases = list(pool.map(lambda _: broker.lease(user.id), range(3)))
        assert len({lease.credential_version for lease in leases}) == 1
        rotated = store.credentials(store.get(user.id, "codex"))
        print(
            "Native refresh changes: access="
            + str(rotated["access_token"] != original["access_token"])
            + " refresh="
            + str(rotated["refresh_token"] != original["refresh_token"])
            + " expiry="
            + str(rotated["expires_at"] != original["expires_at"]),
            flush=True,
        )
        assert rotated["access_token"] != original["access_token"]
        assert all(
            not json.loads(lease.blob()["files"][".codex/auth.json"])["tokens"]["refresh_token"]
            for lease in leases
        )
        print(
            "REAL_GATE Codex: native refresh, rotation and concurrent leases PASS",
            flush=True,
        )
        bearer = PersistentApiKeyStore(auth).create(user_id=user.id)[1]
        with TestClient(
            app, base_url="https://testserver", headers={"Authorization": "Bearer " + bearer}
        ) as client:
            try:
                identifiers = []
                for index in range(3):
                    sid = check(
                        client.post(
                            "/v2/sessions",
                            json={
                                "prompt": f"Create sbx_codex_real_{index}.py. "
                                "Add a greeting function. "
                                "The function returns hello. "
                                "Add and run a standard-library unittest for it. Commit only these "
                                "two files. Do not push, print env, or inspect credentials. "
                                "Report the test result.",
                                "repository": {"repo": repo, "ref": "main"},
                                "execution": {"provider": "codex", "model": "gpt-6.1-sol"},
                            },
                        ),
                        201,
                    )["session"]["id"]
                    identifiers.append(sid)
                    print(f"Real coding Session {index + 1} created", flush=True)
                assert len(identifiers) == 3
                # Force proactive refresh while these three native Sessions are active.
                prior = store.get(user.id, "codex").metadata["credential_version"]
                force_due(store, user.id)
                broker.refresh_due()
                assert store.get(user.id, "codex").metadata["credential_version"] == prior + 1
                for sid in identifiers:
                    wait(client, sid, timeout=600)
                    assert check(client.get(f"/v2/sessions/{sid}/changes/diff"))["files"]
                    agent_id = app.state.task_store.get(sid).agent_id
                    events = read_text(
                        app.state.plane.backend,
                        app.state.plane.get(agent_id).handle(),
                        "events.jsonl",
                    )
                    parsed = [json.loads(line) for line in events.splitlines()]
                    assert any(
                        entry.get("type") == "item.completed"
                        and entry.get("item", {}).get("type") == "command_execution"
                        and "unittest" in entry.get("item", {}).get("command", "")
                        and entry.get("item", {}).get("exit_code") == 0
                        for entry in parsed
                    ), "native_unittest_not_successful"
                    credentials = store.credentials(store.get(user.id, "codex"))
                    assert all(
                        grant[key] not in events
                        for grant in (original, rotated, credentials)
                        for key in ("access_token", "refresh_token", "id_token")
                    )
                print(
                    "REAL_GATE Codex: three real coding Sessions across refresh PASS",
                    flush=True,
                )
                # A next turn gets fresh access, proving continuation beyond the boundary.
                check(
                    client.post(
                        f"/v2/sessions/{identifiers[0]}/messages",
                        json={
                            "prompt": "Run your unittest again and report its result. "
                            "Do not modify files."
                        },
                    ),
                    202,
                )
                wait_run(client, identifiers[0], 2)
                # Exercise the one-retry 401 path with a real native refresh.
                rejected = broker.lease(user.id).credential_version

                def operation(lease):
                    if lease.credential_version == rejected:
                        raise ProviderUnauthorized()
                    return lease

                recovered = broker.execute(user.id, operation)
                assert recovered.credential_version == rejected + 1
                print(
                    "REAL_GATE Codex: real continuation and 401-triggered native rotation PASS",
                    flush=True,
                )
                restored_provider = NativeCodexProvider(bootstrap_home=PRODUCT_HOME)
                restored_provider.bootstrap_owner = user.id
                restored = CodexBroker(
                    ConnectionStore(AuthStore(AuthDatabase(database_url=url)), vault),
                    restored_provider,
                )
                assert restored.lease(user.id).credential_version == recovered.credential_version
                assert (
                    restored.store.credentials(restored.store.get(user.id, "codex"))[
                        "refresh_token"
                    ]
                    == store.credentials(store.get(user.id, "codex"))["refresh_token"]
                )
                print(
                    "REAL_GATE Codex: PostgreSQL reconstruction retains rotation PASS",
                    flush=True,
                )
                # Invalid-grant control flow without revoking the actual reusable account.
                record = store.get(user.id, "codex")
                saved = store.credentials(record)
                damaged = {**saved, "refresh_token": "REDACTED"}
                record.credential_cipher = vault.seal(damaged, context=record.context)
                store.save(record)
                try:
                    broker.lease(user.id, rejected_version=record.metadata["credential_version"])
                except HostedAuthError as error:
                    assert error.code == "codex_reauth_required"
                else:
                    raise AssertionError("invalid_grant_not_refused")
                assert store.get(user.id, "codex").state == "reauth_required"
                provider.import_login(user.id, PRODUCT_HOME)
                print(
                    "REAL_GATE Codex: invalid grant -> reauth_required; login preserved PASS",
                    flush=True,
                )
            finally:
                # The ledger can finish before a watcher completes its last DB
                # writes. Keep the disposable DB/client alive until it exits.
                for thread in threading.enumerate():
                    if thread.name.startswith("sbx-turn-"):
                        thread.join(timeout=120)
                for handle in app.state.plane.backend.list({"owner": user.id}):
                    app.state.plane.backend.terminate(handle)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        frame = traceback.extract_tb(error.__traceback__)[-1]
        print(
            f"REAL_GATE Codex: FAIL {type(error).__name__} "
            f"at {frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}",
            flush=True,
        )
        raise SystemExit(1) from None

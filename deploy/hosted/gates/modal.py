"""Opt-in real Modal gate with PostgreSQL authority and two credentialed workspaces."""

import json
import os
import secrets
import time
import traceback

import httpx
from control.auth_store import AuthDatabase, AuthStore
from control.backend import SandboxSpec
from control.connections import ConnectionStore, SecretVault
from control.hosted_auth import HostedAuthError
from control.hosted_compute import HostedModalBackend
from control.modal_connection import ModalConnectionService
from control.real_modal import RealModalProvider
from deploy.hosted.gates.common import isolated_postgres


def main():
    with isolated_postgres() as url:
        auth = AuthStore(AuthDatabase(database_url=url))
        vault = SecretVault(secrets.token_bytes(32))
        store = ConnectionStore(auth, vault)
        provider = RealModalProvider()
        service = ModalConnectionService(store, provider)
        backend = HostedModalBackend(store, provider)
        users = [auth.create_user(), auth.create_user()]
        handles, runtime = [], []
        try:
            for user, prefix in zip(users, ("SBX_TEST_MODAL", "SBX_TEST_MODAL2")):
                store.connect(
                    user.id,
                    "modal",
                    {
                        "token_id": os.environ[prefix + "_TOKEN_ID"],
                        "token_secret": os.environ[prefix + "_TOKEN_SECRET"],
                    },
                )
                ready = service.provision(user.id)
                assert ready["state"] == "ready"
                assert ready["metadata"]["workspace"] == os.environ[prefix + "_WORKSPACE"]
                assert service.provision(user.id) == ready
                runtime.append(ready)
                handle = backend.create(
                    SandboxSpec(
                        tags={
                            "owner": user.id,
                            "session_id": "modal-real-" + secrets.token_hex(8),
                            "provider": "codex",
                        }
                    )
                )
                handles.append(handle)
                process = backend.exec(
                    handle,
                    [
                        "bash",
                        "-c",
                        "mkdir -p /work/repo && cd /work/repo && git init -q && "
                        "python -m runtime.runner --help >/dev/null && "
                        "python -c \"import os; assert not any(k.startswith('MODAL_TOKEN') "
                        "for k in os.environ); print('runtime bootstrap PASS')\"",
                    ],
                )
                assert process.wait() == 0
                print(
                    "REAL_GATE Modal: workspace Ready, real sandbox, runtime bootstrap PASS",
                    flush=True,
                )
            assert runtime[0]["metadata"]["workspace"] != runtime[1]["metadata"]["workspace"]
            for i, user in enumerate(users):
                found = backend.list({"owner": user.id})
                assert any(h.id == handles[i].id for h in found)
                assert all(h.id != handles[1 - i].id for h in found)
            context, _ = backend._context(users[1].id)
            try:
                provider.poll(context, handles[0])
            except HostedAuthError as error:
                assert error.status == 404
            else:
                raise AssertionError("cross_user_access")
            try:
                provider.sdk.Sandbox.from_id(handles[0].id, client=provider._client(context)).poll()
            except Exception:
                pass
            else:
                raise AssertionError("cross_workspace_provider_access")
            print(
                "REAL_GATE Modal: second workspace SDK cannot access first sandbox PASS", flush=True
            )
            grants = [backend.connect(handle) for handle in handles]
            print("REAL_GATE Modal: two TLS runtime listeners ready", flush=True)
            with httpx.Client(timeout=20, trust_env=False) as http:
                response = http.get(
                    grants[0]["url"],
                    headers={
                        "Authorization": "Bearer " + grants[1]["grant"],
                    },
                )
                assert response.status_code == 401
                print("REAL_GATE Modal: foreign stream grant rejected PASS", flush=True)
                event = {
                    "type": "item.completed",
                    "item": {
                        "id": "modal-stream-gate",
                        "type": "agent_message",
                        "text": "real Modal SSE",
                    },
                }
                writer = backend.exec(
                    handles[0],
                    [
                        "python",
                        "-c",
                        "import time,pathlib; time.sleep(1); "
                        "pathlib.Path('/work/events.jsonl').write_text("
                        f"{json.dumps(event) + chr(10)!r}); "
                        "time.sleep(60)",
                    ],
                )
                start = time.monotonic()
                with http.stream(
                    "GET",
                    grants[0]["url"],
                    headers={
                        "Authorization": "Bearer " + grants[0]["grant"],
                    },
                ) as stream:
                    print("REAL_GATE Modal: direct stream HTTP", stream.status_code, flush=True)
                    assert stream.status_code == 200
                    assert "text/event-stream" in stream.headers["content-type"]
                    for line in stream.iter_lines():
                        if line.startswith("data:"):
                            assert json.loads(line[5:])["item"]["id"] == "modal-stream-gate"
                            break
                    else:
                        raise AssertionError("missing_live_event")
                assert time.monotonic() - start < 12
                print(
                    "REAL_GATE Modal: live event received before writer completion PASS", flush=True
                )
                writer.kill()
                assert writer.wait() != 0
            print(
                "REAL_GATE Modal: real HTTPS incremental SSE, cross-user rejection, cancel PASS",
                flush=True,
            )
            # Reconstruct from PostgreSQL and reacquire the same running sandbox.
            restored = HostedModalBackend(
                ConnectionStore(AuthStore(AuthDatabase(database_url=url)), vault),
                RealModalProvider(),
            )
            assert restored.connect(handles[0])["url"] == grants[0]["url"]
            print("REAL_GATE Modal: PostgreSQL reconstruction and listener reuse PASS", flush=True)
            for handle in handles:
                backend.terminate(handle)
                backend.terminate(handle)
                assert not backend.poll(handle).alive
            handles.clear()
            store.connect(
                users[0].id,
                "modal",
                {
                    "token_id": os.environ["SBX_TEST_MODAL_TOKEN_ID"],
                    "token_secret": os.environ["SBX_TEST_MODAL_TOKEN_SECRET"],
                },
            )
            assert service.provision(users[0].id)["state"] == "ready"
            print("REAL_GATE Modal: idempotent cleanup and reprovision PASS", flush=True)
        finally:
            for handle in handles:
                try:
                    backend.terminate(handle)
                except Exception:
                    print("REAL_GATE Modal: cleanup requires retry", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("REAL_GATE Modal: FAIL", type(error).__name__, flush=True)
        print("Failure line:", traceback.extract_tb(error.__traceback__)[-1].lineno, flush=True)
        print("Failure check:", traceback.extract_tb(error.__traceback__)[-1].line, flush=True)
        raise SystemExit(1) from None

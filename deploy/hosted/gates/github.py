"""Opt-in real App/Git/Modal workflow; deterministic AI until the SOR-292 gate."""

import base64
import os
import secrets
import tempfile
import time
import traceback
from pathlib import Path

import httpx
from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore, PersistentApiKeyStore
from control.connections import SecretVault
from control.real_github import GitHubFactory
from control.real_modal import RealModalProvider, _close_stdin
from deploy.hosted.gates.common import isolated_postgres
from fastapi.testclient import TestClient


class GitGateCompute(RealModalProvider):
    def create(self, context, spec, runtime):
        handle = super().create(context, spec, runtime)
        code = base64.b64encode(Path("tests/fakes/hosted_codex.py").read_bytes()).decode()
        process = self._sandbox(context, handle).exec(
            "python",
            "-c",
            "import base64,pathlib; p=pathlib.Path('/tmp/github-gate-codex.py'); "
            f"p.write_bytes(base64.b64decode({code!r})); p.chmod(0o700)",
        )
        _close_stdin(process)
        assert process.wait() == 0
        return handle

    def exec(self, context, handle, argv, env):
        return super().exec(
            context, handle, argv, {**env, "CODEX_BIN": "/tmp/github-gate-codex.py"}
        )


def check(response, expected=200):
    if response.status_code != expected:
        error = response.json().get("error", {})
        code = error.get("code") if isinstance(error, dict) else error
        raise RuntimeError(f"gate_http_{response.status_code}_{code}")
    return response.json() if response.content else None


def wait(client, identifier, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = check(client.get(f"/v2/sessions/{identifier}"))
        if data["session"]["status"] in ("finished", "failed"):
            if data["session"]["status"] != "finished":
                error = data["session"].get("error") or {}
                print(
                    "Session failed: " + str(error.get("code")) + ": " + str(error.get("source")),
                    flush=True,
                )
                message = error.get("message", "")
                safe = {
                    "repository not found",
                    "task not found",
                    "agent not found",
                    "session not found",
                }
                print(
                    "Session diagnostic: "
                    + (message if message in safe else "uncatalogued message"),
                    flush=True,
                )
                raise RuntimeError("session_failed")
            return data
        time.sleep(0.5)
    raise RuntimeError("session_timeout")


def review(client, identifier):
    rid = check(client.post(f"/hosted/sessions/{identifier}/review-sessions", json={}), 201)[
        "session_id"
    ]
    wait(client, rid)
    data = check(client.get(f"/hosted/review-sessions/{rid}"))
    assert data["status"] == "completed"
    assert data["review"]["independent"]
    return rid, data


def main():
    assert os.environ["SBX_GITHUB_TEST_REPO"] == "soren-labs/sbx-e2e-test"

    factory = GitHubFactory()
    with isolated_postgres() as url, tempfile.TemporaryDirectory(prefix="sbx-github-gate-") as temp:
        auth = AuthStore(AuthDatabase(database_url=url))
        vault = SecretVault(secrets.token_bytes(32))
        # Only this gate subprocess changes HOME; never the development agent's HOME.
        os.environ["HOME"] = temp
        os.environ["SBX_CONNECTIONS_MODE"] = "mock"
        os.environ["SBX_BROWSER_ORIGINS"] = "https://sbx-agent.com"
        compute = GitGateCompute()
        app = create_app(
            auth_store=auth,
            hosted=True,
            state_backend="postgres",
            connection_vault=vault,
            modal_provider=compute,
            compute_provider=compute,
            github_factory=factory,
            runner_cmd=["python", "-m", "runtime.runner"],
        )
        user, other = auth.create_user(), auth.create_user()
        store = app.state.connections
        iid = int(os.environ["SBX_GITHUB_APP_INSTALLATION_ID"])
        repo = os.environ["SBX_GITHUB_TEST_REPO"]
        factory.bind_installation(store, user.id, iid, [repo])
        github = factory(store, user.id)
        assert github.status()["installations"][0]["repositories"] == [repo]
        assert factory(store, other.id).sandbox_token(repo) is None
        token = github.sandbox_token(repo)
        # A private, disposable base branch keeps product E2E changes off main.
        branch = "sbx-real-gate/base-" + secrets.token_hex(6)
        headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
        with httpx.Client(
            base_url="https://api.github.com", headers=headers, timeout=15, trust_env=False
        ) as api:
            info = check(api.get(f"/repos/{repo}"))
            head = check(api.get(f"/repos/{repo}/git/ref/heads/{info['default_branch']}"))[
                "object"
            ]["sha"]
            check(
                api.post(
                    f"/repos/{repo}/git/refs", json={"ref": "refs/heads/" + branch, "sha": head}
                ),
                201,
            )
            hello = api.get(f"/repos/{repo}/contents/hello.txt", params={"ref": branch})
            if hello.status_code == 200:
                check(
                    api.request(
                        "DELETE",
                        f"/repos/{repo}/contents/hello.txt",
                        json={
                            "message": "Reset isolated SBX acceptance greeting",
                            "sha": hello.json()["sha"],
                            "branch": branch,
                        },
                    )
                )
        store.connect(
            user.id,
            "modal",
            {
                "token_id": os.environ["SBX_TEST_MODAL_TOKEN_ID"],
                "token_secret": os.environ["SBX_TEST_MODAL_TOKEN_SECRET"],
            },
        )
        app.state.modal_connections.provision(user.id)
        state = app.state.codex_broker.authorize(user.id)["state"]
        app.state.codex_broker.callback(user.id, state, "mock:" + user.id)
        bearer = PersistentApiKeyStore(auth).create(user_id=user.id)[1]
        try:
            with TestClient(
                app, base_url="https://testserver", headers={"Authorization": "Bearer " + bearer}
            ) as client:
                sid = check(
                    client.post(
                        "/v2/sessions",
                        json={
                            "prompt": "Create hello.txt in the workspace.",
                            "repository": {"repo": repo, "ref": branch},
                            "execution": {"provider": "codex"},
                        },
                    ),
                    201,
                )["session"]["id"]
                wait(client, sid)
                assert check(client.get(f"/v2/sessions/{sid}/changes/diff"))["files"]
                print(
                    "REAL_GATE GitHub: real Modal clone/fetch, commit and tests PASS (AI mocked)",
                    flush=True,
                )
                check(
                    client.post(
                        f"/v2/sessions/{sid}/deliver", json={"pull_request": {"draft": True}}
                    )
                )
                print("REAL_GATE GitHub: real branch push and draft PR PASS", flush=True)
                rid, requested = review(client, sid)
                assert requested["review"]["verdict"] == "request_changes"
                assert client.post(f"/v1/tasks/{sid}/merge", json={}).status_code == 409
                check(
                    client.post(
                        f"/v2/sessions/{sid}/messages",
                        json={"prompt": "Fix the requested greeting"},
                    ),
                    202,
                )
                wait(client, sid)
                check(
                    client.post(
                        f"/v2/sessions/{sid}/deliver", json={"pull_request": {"draft": False}}
                    )
                )
                rid2, passed = review(client, sid)
                assert rid != rid2 and passed["review"]["verdict"] == "approve"
                assert check(client.get(f"/hosted/review-sessions/{rid}"))["review"]["stale"]
                merged = check(client.post(f"/v1/tasks/{sid}/merge", json={}))
                assert merged["revision"]["delivery"]["merged"]
                print(
                    "REAL_GATE GitHub: independent reviews, fix/pass, stale review and merge PASS "
                    "(AI mocked)",
                    flush=True,
                )
                print(
                    "REAL_GATE GitHub: test-repo PR "
                    + str(merged["revision"]["delivery"]["pull_request"]["number"]),
                    flush=True,
                )
                # Revoke only this short-lived token, preserving the shared installation.
                with httpx.Client(
                    base_url="https://api.github.com", headers=headers, trust_env=False
                ) as api:
                    assert api.delete("/installation/token").status_code == 204
                    assert api.get("/installation/repositories").status_code == 401
                github.revoke(iid)
                assert factory(store, user.id).sandbox_token(repo) is None
                print(
                    "REAL_GATE GitHub: actual token revocation and durable owner disconnect PASS",
                    flush=True,
                )
        finally:
            for handle in app.state.plane.backend.list({"owner": user.id}):
                app.state.plane.backend.terminate(handle)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        print(
            "Gate diagnostic HTTP status: "
            + str(getattr(error, "status_code", None))
            + " "
            + str(getattr(error, "operation", None)),
            flush=True,
        )
        frame = next((f for f in reversed(frames) if f.filename.endswith("github.py")), frames[-1])
        print(
            f"REAL_GATE GitHub: FAIL {type(error).__name__} "
            f"at {frame.filename.rsplit('/', 1)[-1]}:{frame.lineno}",
            flush=True,
        )
        raise SystemExit(1) from None

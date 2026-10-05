"""SOR-295: encrypted manual connections, owner isolation and existing runtime seams."""

import json
import secrets
import sys
from pathlib import Path

import httpx
import pytest
from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore, PersistentApiKeyStore
from control.connections import ConnectionStore, SecretVault
from control.hosted_auth import HostedAuthError
from control.hosted_github import HostedGitHub, UserRepoResolver
from control.manual_connections import GitHubTokenProvider, ManualConnections, ZenProvider
from control.tasks import TaskRefusal, canonicalize_repo
from fastapi.testclient import TestClient
from tests.unit.api_v2.conftest import wait_session


@pytest.fixture
def manual(tmp_path):
    auth = AuthStore(AuthDatabase(path=tmp_path / "manual.db"))
    users = [auth.create_user() for _ in range(2)]
    vault = SecretVault(secrets.token_bytes(32))
    credentials = [secrets.token_urlsafe(32) for _ in users]
    invalid = set()
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token in invalid or token not in credentials:
            return httpx.Response(401, json={"message": "REDACTED"})
        index = credentials.index(token)
        slug = f"owner{index}/repo"
        if request.url.path == "/user":
            return httpx.Response(
                200, json={"login": f"owner{index}"}, headers={"x-oauth-scopes": "repo"}
            )
        if request.url.path == "/user/repos":
            return httpx.Response(200, json=[{"full_name": slug, "permissions": {"push": True}}])
        if request.url.path == f"/repos/{slug}":
            return httpx.Response(
                200,
                json={"full_name": slug, "default_branch": "main", "permissions": {"push": True}},
            )
        return httpx.Response(404, json={"message": "REDACTED"})

    github = GitHubTokenProvider(httpx.Client(transport=httpx.MockTransport(handler)))

    class Zen:
        def validate(self, secret):
            if secret in invalid or secret not in credentials:
                raise HostedAuthError("opencode_key_invalid_or_expired_replace_key", 401)
            model = f"opencode/owner{credentials.index(secret)}-free"
            return {"models": [model], "free_models": [model], "default_model": model}

    store = ConnectionStore(auth, vault)
    service = ManualConnections(store, github=github, zen=Zen())
    return auth, users, credentials, invalid, service, calls


@pytest.mark.parametrize("provider", ["github_token", "opencode"])
def test_manual_connect_replace_disable_restart_and_owner_isolation(manual, provider):
    auth, users, credentials, invalid, service, _ = manual
    record = service.connect(users[0].id, provider, credentials[0])
    assert credentials[0] not in json.dumps(record.public())
    assert credentials[0] not in repr(record)
    assert credentials[0].encode() not in auth.database._path.read_bytes()
    assert service.store.get(users[1].id, provider) is None
    other = service.connect(users[1].id, provider, credentials[1])
    assert record.id != other.id
    with pytest.raises(HostedAuthError):
        service.store.vault.open(record.credential_cipher, context=other.context)
    replacement = service.connect(users[0].id, provider, credentials[1])
    assert replacement.id == record.id and replacement.version > record.version
    restored = ManualConnections(
        ConnectionStore(AuthStore(AuthDatabase(path=auth.database._path)), service.store.vault),
        github=service.providers["github_token"],
        zen=service.providers["opencode"],
    )
    assert restored.secret(users[0].id, provider) == credentials[1]
    invalid.add(credentials[1])
    with pytest.raises(HostedAuthError):
        restored.validate(users[0].id, provider)
    assert restored.store.get(users[0].id, provider).state == "invalid"
    with pytest.raises(HostedAuthError):
        restored.secret(users[0].id, provider)
    invalid.clear()
    assert restored.validate(users[0].id, provider).state == "connected"
    restored.disable(users[0].id, provider)
    disabled = service.store.get(users[0].id, provider)
    assert disabled.state == "disabled" and disabled.credential_cipher is None
    assert service.secret(users[1].id, provider) == credentials[1]
    with pytest.raises(HostedAuthError):
        restored.secret(users[0].id, provider)


def test_github_manual_repository_and_delivery_credential_selection(manual):
    _, users, credentials, _, service, _ = manual
    for user, token in zip(users, credentials, strict=True):
        service.connect(user.id, "github_token", token)
    github = HostedGitHub(service.store, manual=service)
    owner = github.for_user(users[0].id)
    assert owner.repositories()[0]["name"] == "owner0/repo"
    assert owner.sandbox_token("https://github.com/owner0/repo") == credentials[0]
    assert owner.sandbox_token("https://github.com/owner1/repo") is None
    assert owner.sandbox_token() is None
    # Existing delivery receives only the current owner's token, including replacement.
    assert owner.git_env("https://github.com/owner0/repo")["GH_TOKEN"] == credentials[0]
    with pytest.raises(TaskRefusal):
        UserRepoResolver(owner).access(canonicalize_repo("owner1/repo"))
    service.disable(users[0].id, "github_token")
    assert github.for_user(users[0].id).repositories() == []
    with pytest.raises(HostedAuthError):
        owner.sandbox_token("https://github.com/owner0/repo")


def test_github_permission_errors_and_transport_errors_do_not_echo_secrets():
    for status, headers in [(401, {}), (403, {}), (200, {"x-oauth-scopes": "read:user"})]:
        provider = GitHubTokenProvider(
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        status, headers=headers, json={"login": "owner", "message": "REDACTED"}
                    )
                )
            )
        )
        with pytest.raises(HostedAuthError) as error:
            provider.validate("REDACTED")
        assert "REDACTED" not in str(error.value)
        assert "token" in error.value.code


def test_zen_catalog_proves_access_and_prefers_free_without_probing_paid():
    probes = []

    def handler(request):
        if request.url.host == "models.dev":
            return httpx.Response(
                200,
                json={
                    "opencode": {
                        "npm": "@ai-sdk/openai-compatible",
                        "models": {
                            model: {"tool_call": True, "cost": {"input": cost, "output": cost}}
                            for model, cost in [("free", 0), ("blocked", 0), ("paid", 1)]
                        },
                    }
                },
            )
        if request.method == "GET":
            return httpx.Response(
                200, json={"data": [{"id": m} for m in ["paid", "free", "blocked", "unknown"]]}
            )
        model = json.loads(request.content)["model"]
        probes.append(model)
        return httpx.Response(403 if model == "blocked" else 200, json={})

    zen = ZenProvider(httpx.Client(transport=httpx.MockTransport(handler)))
    result = zen.validate("REDACTED")
    assert result["models"] == result["free_models"] == ["opencode/free"]
    assert result["default_model"] == "opencode/free"
    assert probes == ["blocked", "free"]
    assert "REDACTED" not in json.dumps(result)


def test_zen_models_listing_is_not_sufficient_authentication():
    def handler(request):
        if request.url.host == "models.dev":
            return httpx.Response(
                200,
                json={
                    "opencode": {
                        "models": {
                            "free": {
                                "provider": {"npm": "@ai-sdk/openai-compatible"},
                                "tool_call": True,
                                "cost": {"input": 0, "output": 0},
                            }
                        }
                    }
                },
            )
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "free"}]})
        return httpx.Response(401, json={"message": "REDACTED"})

    with pytest.raises(HostedAuthError, match="invalid_or_expired"):
        ZenProvider(httpx.Client(transport=httpx.MockTransport(handler))).validate("REDACTED")


def test_manual_http_status_replace_revoke_restart_and_model_owner_isolation(manual, monkeypatch):
    auth, users, credentials, _, service, _ = manual
    monkeypatch.setenv("SBX_CONNECTIONS_MODE", "mock")
    headers = [
        {"Authorization": f"Bearer {PersistentApiKeyStore(auth).create(user_id=u.id)[1]}"}
        for u in users
    ]

    def factory():
        return create_app(
            auth_store=AuthStore(AuthDatabase(path=auth.database._path)),
            hosted=True,
            state_backend="postgres",
            connection_vault=service.store.vault,
            github_token_provider=service.providers["github_token"],
            zen_provider=service.providers["opencode"],
        )

    with TestClient(factory()) as client:
        for i, token in enumerate(credentials):
            for provider, field in [("github", "token"), ("opencode", "api_key")]:
                response = client.post(
                    f"/hosted/connections/{provider}", json={field: token}, headers=headers[i]
                )
                assert response.status_code == 200, response.text
                assert token not in response.text
        assert (
            client.get("/hosted/repositories", headers=headers[0]).json()["repositories"][0]["name"]
            == "owner0/repo"
        )
        models = client.get("/v1/models", headers=headers[0]).json()
        assert "owner0-free" in json.dumps(models) and "owner1-free" not in json.dumps(models)
        assert client.get("/hosted/connections/opencode").status_code == 401
        assert (
            client.post(
                "/hosted/connections/github", json={"token": "REDACTED"}, headers=headers[0]
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/hosted/connections/opencode", json={"api_key": ""}, headers=headers[0]
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/hosted/connections/github/validate", json={}, headers=headers[0]
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/hosted/connections/opencode/validate", json={}, headers=headers[0]
            ).status_code
            == 200
        )
    with TestClient(factory()) as client:
        assert (
            client.get("/hosted/connections/github", headers=headers[0]).json()["connection"][
                "state"
            ]
            == "connected"
        )
        for provider in ("github", "opencode"):
            response = client.request(
                "DELETE", f"/hosted/connections/{provider}", json={}, headers=headers[0]
            )
            assert response.status_code == 200
            assert (
                client.get(f"/hosted/connections/{provider}", headers=headers[1]).json()[
                    "connection"
                ]["state"]
                == "connected"
            )


def test_zen_real_runner_launch_followup_materialization_cleanup_no_codex(manual, monkeypatch):
    auth, users, credentials, _, service, _ = manual
    monkeypatch.setenv("SBX_CONNECTIONS_MODE", "mock")
    monkeypatch.setenv("OPENCODE_BIN", str(Path("tests/fakes/fake_opencode.py").resolve()))
    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    app = create_app(
        auth_store=auth,
        hosted=True,
        state_backend="postgres",
        connection_vault=service.store.vault,
        zen_provider=service.providers["opencode"],
        runner_cmd=[sys.executable, "-m", "runtime.runner"],
    )
    app.state.connections.connect(
        users[0].id, "modal", {"token_id": "REDACTED", "token_secret": "REDACTED"}
    )
    app.state.modal_connections.provision(users[0].id)
    app.state.manual_connections.connect(users[0].id, "opencode", credentials[0])
    headers = {
        "Authorization": f"Bearer {PersistentApiKeyStore(auth).create(user_id=users[0].id)[1]}"
    }
    with TestClient(app) as client:
        response = client.post("/v2/sessions", json={"prompt": "Create hello.txt"}, headers=headers)
        assert response.status_code == 201, response.text
        session_id = response.json()["session"]["id"]
        result = wait_session(client, headers, session_id, "finished", "failed")
        assert result["session"]["status"] == "finished", result
        task = app.state.task_store.get(session_id)
        assert task.resolved["execution"]["provider"] == "opencode"
        assert task.resolved["execution"]["model"] == "opencode/owner0-free"
        handle = app.state.plane.store.get(task.agent_id).handle()
        assert not (handle.root / "home/.local/share/opencode/auth.json").exists()
        assert app.state.connections.get(users[0].id, "codex") is None
        assert (
            client.post(
                f"/v2/sessions/{session_id}/messages", json={"prompt": "Continue"}, headers=headers
            ).status_code
            == 202
        )
        result = wait_session(client, headers, session_id, "finished", "failed")
        assert result["session"]["status"] == "finished", result
        assert not (handle.root / "home/.local/share/opencode/auth.json").exists()
        assert (
            credentials[0]
            not in client.get(f"/v2/sessions/{session_id}/history", headers=headers).text
        )


def test_runtime_redaction_handles_unformatted_manual_keys(manual, monkeypatch):
    from runtime.runner.events import redact_line, redact_text

    _, users, credentials, _, service, _ = manual
    service.connect(users[0].id, "opencode", credentials[0])
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", json.dumps(service.blob(users[0].id)))
    monkeypatch.setenv("GH_TOKEN", credentials[1])
    assert credentials[0] not in redact_text(f"CLI printed {credentials[0]}")
    assert credentials[1] not in redact_line(json.dumps({"text": credentials[1]}))


def test_runtime_credential_materialization_and_backend_owner_selection(manual, tmp_path):
    from control.backend import SandboxHandle
    from control.codex_broker import CodexBroker, UnconfiguredCodexProvider
    from control.hosted_github import GitHubScopedBackend
    from control.workspace import InMemoryWorkspaceStore
    from runtime.runner.credentials import restore_credential_blob

    _, users, credentials, _, service, _ = manual
    record = service.connect(users[0].id, "opencode", credentials[0])
    captured = []

    class Process:
        stdout = []

        def wait(self):
            return 0

    class Backend:
        def exec(self, handle, argv, env=None):
            captured.append(env)
            return Process()

    backend = GitHubScopedBackend(
        Backend(), HostedGitHub(service.store, manual=service), InMemoryWorkspaceStore()
    )
    backend.codex_broker = CodexBroker(service.store, UnconfiguredCodexProvider())
    handle = SandboxHandle(
        "sandbox", tmp_path, {"owner": users[0].id, "provider": "opencode", "account_id": record.id}
    )
    backend.exec(handle, ["runner", "init"], env={"SBX_ACCOUNT_CREDENTIAL": "REDACTED"})
    blob = json.loads(captured[0]["SBX_ACCOUNT_CREDENTIAL"])
    assert blob == service.blob(users[0].id) and blob["provider"] == "opencode"
    assert captured[0]["SBX_ACCOUNT_ID"] == record.id
    home = tmp_path / "home"
    paths = restore_credential_blob(home, provider="opencode", env=captured[0])
    assert json.loads(paths[0].read_text()) == {"opencode": {"type": "api", "key": credentials[0]}}
    assert paths[0].stat().st_mode & 0o777 == 0o600
    other = SandboxHandle("other", tmp_path, {"owner": users[1].id, "provider": "opencode"})
    with pytest.raises(HostedAuthError, match="connection_required"):
        backend.exec(other, ["runner", "turn"])


def test_manual_github_remote_permission_failure_is_actionable_and_secret_safe():
    from control.github_remote import RemoteGitHubError
    from control.manual_connections import TokenGitHubRemote

    remote = TokenGitHubRemote(
        "REDACTED",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(403, json={"message": "REDACTED"})
            )
        ),
    )
    with pytest.raises(RemoteGitHubError) as error:
        remote.create_pull("owner/repo", head="branch", base="main", title="Test")
    assert "Contents and Pull requests read/write" in str(error.value)
    assert "REDACTED" not in str(error.value)


def test_ineligible_zen_does_not_advertise_fallback_models(manual):
    from control.codex_broker import CodexBroker, UnconfiguredCodexProvider
    from control.hosted_accounts import HostedAccounts, HostedCapabilities

    _, users, credentials, invalid, service, _ = manual
    service.connect(users[0].id, "opencode", credentials[0])
    accounts = HostedAccounts(CodexBroker(service.store, UnconfiguredCodexProvider()), users[0].id)
    account = accounts.list()[0]
    accounts.mark_status(account.id, "invalid", last_error="auth_invalid")
    assert service.store.get(users[0].id, "opencode").state == "invalid"
    assert HostedCapabilities(accounts).get(accounts.get(account.id)).models == ()
    assert service.validate(users[0].id, "opencode").state == "connected"
    assert accounts.get(account.id).status == "active"


def test_manual_token_delivery_create_review_merge_uses_owner_token(manual, monkeypatch):
    import hashlib

    from control.artifacts import ArtifactManifest, InMemoryArtifactStore
    from control.hosted_delivery import owner_revision_service
    from control.revisions import InMemoryRevisionStore, Revision, RevisionService

    _, users, credentials, _, manual_service, _ = manual
    manual_service.connect(users[0].id, "github_token", credentials[0])
    service = HostedGitHub(manual_service.store, manual=manual_service).for_user(users[0].id)
    pulls = []
    observed = []
    head, base = "b" * 40, "a" * 40

    def handler(request):
        observed.append(request.headers["authorization"])
        if request.url.path == "/repos/owner0/repo":
            return httpx.Response(200, json={"full_name": "owner0/repo"})
        if request.method == "GET" and request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=pulls)
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            body = json.loads(request.content)
            pulls.append(
                {
                    "number": 1,
                    "html_url": "https://github.com/owner0/repo/pull/1",
                    "state": "open",
                    "draft": False,
                    "head": {"sha": head, "ref": body["head"]},
                    "base": {"ref": "main"},
                }
            )
            return httpx.Response(201, json=pulls[0])
        if request.url.path.endswith("/merge"):
            return httpx.Response(200, json={"merged": True, "sha": head})
        return httpx.Response(200, json=pulls[0])

    manual_service.providers["github_token"].client = httpx.Client(
        transport=httpx.MockTransport(handler)
    )
    patch = b"diff --git a/file b/file\n+hello\n"
    artifacts, store = InMemoryArtifactStore(), InMemoryRevisionStore()
    artifact = ArtifactManifest(
        artifact_id="artifact",
        base_sha=base,
        head_sha=head,
        repo="https://github.com/owner0/repo",
        created_at="t",
        producer_agent_id="author",
        payloads={"patch.diff": hashlib.sha256(patch).hexdigest()},
    )
    artifacts.put(artifact, {"patch.diff": patch})
    revision = Revision(
        revision_id="revision",
        agent_id="author",
        n=1,
        repo=artifact.repo,
        base_sha=base,
        head_sha=head,
        artifact_id="artifact",
        created_at="t",
        updated_at="t",
    )
    store.put_revision(revision)

    def push(repo, branch, **kwargs):
        assert kwargs["env"]["GH_TOKEN"] == credentials[0]
        return head

    monkeypatch.setattr("control.revisions.push_payload", push)
    monkeypatch.setattr("control.github_remote.ls_remote", lambda *a, **k: head)
    engine = owner_revision_service(RevisionService(store, artifacts), service)
    revision = engine.deliver(
        revision, overrides={"pull_request": {"title": "Manual token change", "draft": False}}
    )
    engine.add_review(
        revision,
        reviewer_identity="agent:reviewer",
        reviewer_agent_id="reviewer",
        verdict="approve",
    )
    assert engine.merge(revision).delivery["merged"] is True
    assert observed and set(observed) == {"Bearer " + credentials[0]}


def test_independent_review_inherits_author_zen_provider(manual, monkeypatch):
    from types import SimpleNamespace

    from control import hosted_reviews

    _, users, _, _, service, _ = manual
    task = SimpleNamespace(
        request={"name": "Author"}, resolved={"execution": {"provider": "opencode"}}
    )
    revision = SimpleNamespace(
        repo="https://github.com/owner/repo",
        head_sha="b" * 40,
        base_sha="a" * 40,
        revision_id="revision",
        delivery={"branch": "delivery"},
    )
    monkeypatch.setattr(hosted_reviews, "author_revision", lambda *a: (task, revision, None))
    for name in (
        "get_plane",
        "get_v1_state",
        "get_run_states",
        "get_registry",
        "get_scheduler",
        "get_run_reporter",
        "get_workflow_service",
        "get_resources",
        "get_capabilities",
        "get_task_store",
        "get_repo_resolver",
    ):
        monkeypatch.setattr(hosted_reviews.deps, name, lambda *args: None)
    captured = []
    monkeypatch.setattr(
        hosted_reviews,
        "create_session",
        lambda body, *a, **k: captured.append(body) or {"session": {"id": "reviewer"}},
    )
    records = SimpleNamespace(put_owned=lambda *a: None)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(database_records=records, auth_store=service.store.auth)
        )
    )
    hosted_reviews.start_review(request, SimpleNamespace(id=users[0].id), "author")
    assert captured[0].execution.provider == "opencode"


def test_revoked_manual_github_session_preflight_has_actionable_error(manual, monkeypatch):
    auth, users, credentials, _, service, _ = manual
    monkeypatch.setenv("SBX_CONNECTIONS_MODE", "mock")
    app = create_app(
        auth_store=auth,
        hosted=True,
        state_backend="postgres",
        connection_vault=service.store.vault,
        github_token_provider=service.providers["github_token"],
        zen_provider=service.providers["opencode"],
    )
    app.state.manual_connections.connect(users[0].id, "opencode", credentials[0])
    app.state.connections.connect(
        users[0].id, "modal", {"token_id": "REDACTED", "token_secret": "REDACTED"}
    )
    app.state.modal_connections.provision(users[0].id)
    app.state.manual_connections.connect(users[0].id, "github_token", credentials[0])
    app.state.manual_connections.disable(users[0].id, "github_token")
    headers = {
        "Authorization": f"Bearer {PersistentApiKeyStore(auth).create(user_id=users[0].id)[1]}"
    }
    with TestClient(app) as client:
        response = client.post(
            "/v2/sessions",
            json={"prompt": "Hi", "repository": {"repo": "owner0/repo"}},
            headers=headers,
        )
        assert response.status_code in {201, 403}
        if response.status_code == 201:
            result = wait_session(client, headers, response.json()["session"]["id"], "failed")
            assert "Integrations" in json.dumps(result)
        else:
            assert "Integrations" in response.text

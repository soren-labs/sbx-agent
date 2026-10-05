"""Production env contracts and the cloud-free mock launcher."""

import base64
import importlib.util
import secrets
from pathlib import Path

import pytest
from control.hosted_server import production_config
from fastapi.testclient import TestClient


def production_env():
    return {
        "SBX_HOSTED": "1",
        "SBX_STATE_BACKEND": "postgres",
        "DATABASE_URL": "postgresql://REDACTED",
        "SBX_BROWSER_ORIGINS": "https://sbx-agent.com",
        "SBX_CONNECTIONS_MODE": "production",
        "SBX_AUTH_EMAIL_MODE": "production",
        "SBX_HOSTED_ADAPTER_FACTORY": "deployment_adapters:create_adapters",
        "SBX_CONNECTION_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    }


@pytest.mark.parametrize(
    "name",
    [
        "SBX_HOSTED",
        "SBX_STATE_BACKEND",
        "DATABASE_URL",
        "SBX_BROWSER_ORIGINS",
        "SBX_CONNECTIONS_MODE",
        "SBX_AUTH_EMAIL_MODE",
        "SBX_HOSTED_ADAPTER_FACTORY",
    ],
)
def test_production_requires_explicit_env(name):
    env = production_env()
    production_config(env)
    del env[name]
    with pytest.raises(ValueError):
        production_config(env)


def test_production_rejects_mock_and_default_operator_auth():
    env = production_env()
    env["SBX_CONNECTIONS_MODE"] = "mock"
    with pytest.raises(ValueError):
        production_config(env)
    env = production_env()
    env.update(SBX_OPERATOR_AUTH_ENABLED="1", SBX_BASIC_USER="sbx", SBX_BASIC_PASS="sbx")
    with pytest.raises(ValueError):
        production_config(env)


def test_mock_launcher_private_key_and_email_inbox_reconstruction(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "mock_hosted_launcher", Path("deploy/hosted/mock_server.py")
    )
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    # The launcher clears the environment, restored by pytest's monkeypatch.
    monkeypatch.setattr(launcher.os, "environ", dict(launcher.os.environ))
    app = launcher.build_app(tmp_path)
    key = (tmp_path / "connection-vault.key").read_bytes()
    assert (tmp_path / "connection-vault.key").stat().st_mode & 0o777 == 0o600
    with TestClient(app) as client:
        result = client.post("/auth/register", json={"email": "launcher@example.test"})
        assert result.status_code == 202
        inbox = client.get(f"/dev/email-inbox/{result.json()['challenge_id']}")
        assert inbox.status_code == 200
        assert len(inbox.json()["code"]) == 6
        assert client.get("/dev/email-inbox/missing").status_code == 404
    restored = launcher.build_app(tmp_path)
    assert (tmp_path / "connection-vault.key").read_bytes() == key
    assert restored.state.auth_store.database._path == app.state.auth_store.database._path


def test_real_factory_uses_one_user_scoped_compute_adapter(monkeypatch):
    from control import production_adapters as factory

    instances = {name: object() for name in ("compute", "email", "github", "codex")}
    monkeypatch.setattr(factory, "RealModalProvider", lambda: instances["compute"])
    monkeypatch.setattr(factory.ResendEmailSender, "from_env", lambda: instances["email"])
    monkeypatch.setattr(factory, "GitHubFactory", lambda: instances["github"])
    monkeypatch.setattr(factory, "NativeCodexProvider", lambda: instances["codex"])
    adapters = factory.create_adapters()
    assert adapters["compute_provider"] is adapters["modal_provider"] is instances["compute"]
    assert set(adapters) == {
        "email_sender",
        "modal_provider",
        "compute_provider",
        "github_factory",
        "codex_provider",
    }


def test_production_runs_user_image_python_instead_of_vps_virtualenv(monkeypatch):
    import importlib

    from control import hosted_server, production_adapters

    app = importlib.import_module("control.app")

    for name, value in production_env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SBX_HOSTED_ADAPTER_FACTORY", "control.production_adapters:create_adapters")
    adapters = {
        name: object()
        for name in (
            "email_sender",
            "modal_provider",
            "compute_provider",
            "github_factory",
            "codex_provider",
        )
    }
    monkeypatch.setattr(production_adapters, "create_adapters", lambda: adapters)
    monkeypatch.setattr(app, "create_app", lambda **kwargs: kwargs)
    result = hosted_server.create_hosted_app()
    assert result["runner_cmd"] == ["python", "-m", "runtime.runner"]
    assert result["max_concurrent"] == 5


def test_production_factory_supports_manual_github_without_app_configuration(monkeypatch):
    from control import production_adapters

    for key in ("SBX_GITHUB_APP_ID", "SBX_GITHUB_APP_SLUG", "SBX_GITHUB_APP_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(production_adapters, "RealModalProvider", lambda: object())
    monkeypatch.setattr(production_adapters.ResendEmailSender, "from_env", lambda: object())
    monkeypatch.setattr(production_adapters, "NativeCodexProvider", lambda: object())
    assert production_adapters.create_adapters()["github_factory"] is None

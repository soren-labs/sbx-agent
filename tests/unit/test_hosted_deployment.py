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

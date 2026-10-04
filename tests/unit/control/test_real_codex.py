import json
import secrets
import time
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from control.auth_store import AuthDatabase, AuthStore
from control.codex_broker import CodexBroker, InvalidGrant
from control.codex_process import AccessOnlyProcess, RetryCodexProcess
from control.connections import ConnectionStore, SecretVault
from control.hosted_auth import HostedAuthError
from control.real_codex import NativeCodexProvider, write_native_auth


def credentials():
    return {
        "access_token": jwt.encode({"exp": time.time() + 3600}, secrets.token_bytes(32)),
        "refresh_token": "REDACTED",
        "id_token": "REDACTED",
        "account_id": "account",
        "expires_at": time.time() + 3600,
    }


class RPC:
    calls = []
    notifications = []
    failure = None

    def __init__(self, home, binary):
        self.home = home

    def call(self, method, params):
        self.calls.append((method, params))
        if self.failure:
            raise self.failure
        if method == "account/login/start":
            return {
                "verificationUrl": "https://auth.openai.com/codex/device",
                "userCode": "REDACTED",
            }
        if params.get("refreshToken"):
            write_native_auth(self.home, credentials())
        return {"account": {"type": "chatgpt"}}

    def events(self):
        return self.notifications

    def close(self):
        pass


@pytest.fixture
def env(tmp_path):
    store = ConnectionStore(
        AuthStore(AuthDatabase(path=tmp_path / "auth.db")), SecretVault(secrets.token_bytes(32))
    )
    owner = store.auth.create_user().id
    provider = NativeCodexProvider(
        binary="REDACTED",
        root=tmp_path / "memory",
        rpc_factory=RPC,
        import_root=tmp_path / "approved",
    )
    broker = CodexBroker(store, provider)
    home = tmp_path / "approved" / "product"
    home.mkdir(parents=True, mode=0o700)
    write_native_auth(home, credentials())
    return store, owner, provider, broker, home


def test_native_import_encryption_access_only_and_dev_login_rejection(env, monkeypatch):
    store, owner, provider, broker, home = env
    original = (home / "auth.json").read_bytes()
    record = provider.import_login(owner, home)
    assert record.state == "connected"
    lease = broker.lease(owner)
    tokens = json.loads(lease.blob()["files"][".codex/auth.json"])["tokens"]
    assert not tokens["refresh_token"]
    native = json.loads(lease.blob()["files"][".codex/auth.json"])
    assert native["auth_mode"] == "chatgptAuthTokens" and native["last_refresh"]
    assert (home / "auth.json").read_bytes() == original
    assert (
        store.credentials(record)["access_token"].encode()
        not in store.auth.database._path.read_bytes()
    )
    monkeypatch.setenv("HOME", str(home.parent))
    with pytest.raises(ValueError):
        provider.import_login(owner, Path.home() / ".codex")


def test_native_refresh_is_broker_only_and_transient_material_is_removed(env):
    store, owner, provider, broker, home = env
    provider.import_login(owner, home)
    original = (home / "auth.json").read_bytes()
    result = broker.lease(owner, rejected_version=1)
    assert result.credential_version == 2
    assert ("account/read", {"refreshToken": True}) in RPC.calls
    assert (home / "auth.json").read_bytes() == original
    assert list(provider.root.iterdir()) == []


def test_device_authorization_is_owner_bound_and_consumed_once(env):
    store, owner, provider, broker, home = env
    authorization = broker.authorize(owner)
    state = authorization["state"]
    assert authorization["user_code"] == "REDACTED"
    with pytest.raises(HostedAuthError):
        provider.poll("other", state)
    assert not provider.poll(owner, state)
    pending = provider.pending[state]
    write_native_auth(pending["home"], credentials())
    pending["rpc"].notifications = [
        {"method": "account/login/completed", "params": {"success": True}}
    ]
    assert provider.poll(owner, state)
    assert broker.callback(owner, state, state).state == "connected"
    assert not provider.pending and not list(provider.root.iterdir())
    with pytest.raises(HostedAuthError):
        broker.callback(owner, state, state)


def test_invalid_native_grant_changes_state_without_token_material_in_public_view(env, monkeypatch):
    store, owner, provider, broker, home = env
    provider.import_login(owner, home)
    monkeypatch.setattr(RPC, "failure", InvalidGrant())
    with pytest.raises(HostedAuthError, match="codex_reauth_required"):
        broker.lease(owner, rejected_version=1)
    record = store.get(owner, "codex")
    assert record.state == "reauth_required"
    assert "refresh_token" not in json.dumps(record.public())


@pytest.mark.parametrize("permanent", [False, True])
def test_native_cached_account_does_not_hide_refresh_failure(env, monkeypatch, permanent):
    store, owner, provider, broker, home = env
    provider.import_login(owner, home)

    def unchanged(self, method, params):
        if method == "getAuthStatus":
            return {"authToken": None if permanent else "REDACTED"}
        return {"account": {"type": "chatgpt"}}

    monkeypatch.setattr(RPC, "call", unchanged)
    with pytest.raises(HostedAuthError) as caught:
        broker.lease(owner, rejected_version=1)
    expected = "codex_reauth_required" if permanent else "codex_refresh_unavailable"
    assert caught.value.code == expected
    assert store.get(owner, "codex").metadata["credential_version"] == 1
    assert list(provider.root.iterdir()) == []


def test_runtime_retries_auth_once_and_rejects_second_auth_failure():
    def process(code):
        return SimpleNamespace(
            stdout=iter(["event"]),
            wait=lambda: code,
            kill=lambda: None,
            stderr_text=lambda limit: "",
        )

    refreshes, rejects = [], []

    def refresh(version):
        refreshes.append(version)
        return process(5), version + 1

    runtime = RetryCodexProcess(process(5), 1, refresh, rejects.append)
    assert list(runtime.stdout) == ["event", "event"]
    assert runtime.wait() == 5
    assert refreshes == [1] and rejects == [2]
    runtime = RetryCodexProcess(process(5), 1, lambda version: (process(0), 2), rejects.append)
    assert runtime.wait() == 0
    assert rejects == [2]


def test_native_access_cache_cleanup_runs_once_on_finish_or_cancel():
    cleaned = []
    process = SimpleNamespace(stdout=iter([]), wait=lambda: 0, kill=lambda: None)
    scoped = AccessOnlyProcess(process, lambda: cleaned.append("cleared"))
    assert scoped.wait() == 0
    assert scoped.wait() == 0
    assert cleaned == ["cleared"]
    scoped = AccessOnlyProcess(process, lambda: cleaned.append("cancelled"))
    scoped.kill()
    assert scoped.wait() == 0
    assert cleaned == ["cleared", "cancelled"]


def test_missing_native_executable_is_not_reported_as_configured(tmp_path):
    with pytest.raises(ValueError, match="official Codex CLI"):
        NativeCodexProvider(binary=str(tmp_path / "missing-codex"))

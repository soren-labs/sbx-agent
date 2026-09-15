"""``POST /v1/accounts/{id}/verify`` (SOR-80).

Provider-aware ``runner init``: the account's named Secret is attached to the
throwaway sandbox, a local registry blob travels via
``SBX_ACCOUNT_CREDENTIAL``, and an empty/absent blob must never shadow the
Secret. The throwaway sandbox is always terminated.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from control.backend import SandboxHandle, SandboxSpec
from tests.unit.api_v1.conftest import seed_account


class RecordingBackend:
    """Wraps a real backend; records ``create`` specs, ``exec`` argv/env, and
    ``terminate`` calls while delegating to ``LocalProcessBackend`` so the
    stub runner still executes ``init`` for real."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.specs: list[SandboxSpec] = []
        self.execs: list[tuple[list[str], dict[str, str]]] = []
        self.terminated: list[str] = []

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        self.specs.append(spec)
        return self._inner.create(spec)

    def exec(self, handle: SandboxHandle, argv: list[str], env: Any = None) -> Any:
        self.execs.append((list(argv), dict(env or {})))
        return self._inner.exec(handle, argv, env=env)

    def terminate(self, handle: SandboxHandle) -> None:
        self.terminated.append(handle.id)
        return self._inner.terminate(handle)

    def poll(self, handle: SandboxHandle) -> Any:
        return self._inner.poll(handle)

    def list(self, tags: Any = None) -> Any:
        return self._inner.list(tags)


class _FakeProc:
    def __init__(self, code: int) -> None:
        self._code = code
        self.stdout: Iterator[str] = iter(())

    def wait(self) -> int:
        return self._code

    def kill(self) -> None:
        pass


class FakeVerifyBackend:
    """Deterministic backend for verify: fixed init exit code or failures."""

    def __init__(
        self,
        root: Path,
        code: int = 0,
        *,
        exec_error: Exception | None = None,
        terminate_error: Exception | None = None,
    ) -> None:
        self._root = root
        self._code = code
        self._exec_error = exec_error
        self._terminate_error = terminate_error
        self.specs: list[SandboxSpec] = []
        self.execs: list[tuple[list[str], dict[str, str]]] = []
        self.terminated: list[str] = []

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        self.specs.append(spec)
        return SandboxHandle(id="verify-sb", root=self._root, tags=dict(spec.tags))

    def exec(self, handle: SandboxHandle, argv: list[str], env: Any = None) -> _FakeProc:
        self.execs.append((list(argv), dict(env or {})))
        if self._exec_error is not None:
            raise self._exec_error
        return _FakeProc(self._code)

    def terminate(self, handle: SandboxHandle) -> None:
        self.terminated.append(handle.id)
        if self._terminate_error is not None:
            raise self._terminate_error

    def poll(self, handle: SandboxHandle) -> Any:
        return None

    def list(self, tags: Any = None) -> list[Any]:
        return []


@pytest.fixture
def spy_backend(v1_env) -> RecordingBackend:
    spy = RecordingBackend(v1_env.backend)
    v1_env.app.state.plane.backend = spy
    return spy


def _argv_opt(argv: list[str], flag: str) -> str:
    assert flag in argv, f"{flag} missing from {argv}"
    return argv[argv.index(flag) + 1]


class TestVerifyCredentialAttach:
    @pytest.mark.parametrize(
        ("provider", "account_id", "secret_name", "expected_model"),
        [
            ("devin", "acct-devin-v", "sbx-acct-devin-v", "swe-2-high"),
            ("antigravity", "acct-agy-v", "sbx-acct-agy-v", "gemini-3.8-flash-low"),
            ("grok", "acct-grok-v", "sbx-acct-grok-v", "grok-4.6"),
        ],
    )
    def test_secret_only_account_verifies_with_named_secret(
        self,
        client,
        admin_auth,
        v1_env,
        spy_backend,
        provider,
        account_id,
        secret_name,
        expected_model,
    ) -> None:
        seed_account(v1_env, account_id, provider=provider, secret_name=secret_name)
        resp = client.post(f"/v1/accounts/{account_id}/verify", headers=admin_auth)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "active"
        assert body["last_error"] is None

        assert len(spy_backend.specs) == 1
        spec = spy_backend.specs[0]
        assert spec.secrets == [secret_name]
        assert spec.tags == {"purpose": "account-verify", "account_id": account_id}

        assert len(spy_backend.execs) == 1
        argv, env = spy_backend.execs[0]
        assert "init" in argv
        assert _argv_opt(argv, "--provider") == provider
        assert _argv_opt(argv, "--account-id") == account_id
        assert _argv_opt(argv, "--model") == expected_model
        assert env["SBX_ACCOUNT_ID"] == account_id
        # An empty blob must not shadow the attached Secret.
        assert "SBX_ACCOUNT_CREDENTIAL" not in env

        assert len(spy_backend.terminated) == 1

    def test_local_registry_blob_travels_via_env(
        self, client, admin_auth, v1_env, spy_backend
    ) -> None:
        seed_account(v1_env, "acct-grok-blob", provider="grok")
        blob = {"provider": "grok", "files": {".grok/auth.json": "REDACTED"}}
        v1_env.registry.put_credential_blob("acct-grok-blob", blob)
        resp = client.post("/v1/accounts/acct-grok-blob/verify", headers=admin_auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "active"

        argv, env = spy_backend.execs[0]
        assert _argv_opt(argv, "--provider") == "grok"
        assert _argv_opt(argv, "--account-id") == "acct-grok-blob"
        assert _argv_opt(argv, "--model") == "grok-4.6"
        assert json.loads(env["SBX_ACCOUNT_CREDENTIAL"]) == blob
        assert spy_backend.specs[0].secrets == []
        assert len(spy_backend.terminated) == 1

    def test_named_secret_attached_even_with_local_blob(
        self, client, admin_auth, v1_env, spy_backend
    ) -> None:
        seed_account(v1_env, "acct-devin-both", provider="devin", secret_name="sbx-acct-devin-both")
        blob = {"provider": "devin", "files": {".devin/credentials.toml": "REDACTED"}}
        v1_env.registry.put_credential_blob("acct-devin-both", blob)
        resp = client.post("/v1/accounts/acct-devin-both/verify", headers=admin_auth)
        assert resp.status_code == 200, resp.text

        spec = spy_backend.specs[0]
        assert spec.secrets == ["sbx-acct-devin-both"]
        _, env = spy_backend.execs[0]
        assert json.loads(env["SBX_ACCOUNT_CREDENTIAL"]) == blob

    def test_empty_credential_injects_no_blob(
        self, client, admin_auth, v1_env, spy_backend
    ) -> None:
        # No Secret, no stored blob: codex keeps the CODEX_AUTH_JSON path and
        # init must not receive an SBX_ACCOUNT_CREDENTIAL placeholder.
        seed_account(v1_env, "acct-codex-empty", provider="codex")
        resp = client.post("/v1/accounts/acct-codex-empty/verify", headers=admin_auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "active"

        argv, env = spy_backend.execs[0]
        assert _argv_opt(argv, "--provider") == "codex"
        assert _argv_opt(argv, "--model") == "gpt-5.6-luna"
        assert "SBX_ACCOUNT_CREDENTIAL" not in env
        assert spy_backend.specs[0].secrets == []

    def test_empty_stored_blob_does_not_shadow_secret(
        self, client, admin_auth, v1_env, spy_backend
    ) -> None:
        seed_account(v1_env, "acct-grok-empty", provider="grok", secret_name="sbx-acct-g")
        v1_env.registry.put_credential_blob("acct-grok-empty", {})
        resp = client.post("/v1/accounts/acct-grok-empty/verify", headers=admin_auth)
        assert resp.status_code == 200, resp.text

        assert spy_backend.specs[0].secrets == ["sbx-acct-g"]
        _, env = spy_backend.execs[0]
        assert "SBX_ACCOUNT_CREDENTIAL" not in env


class TestVerifyExitCodes:
    def test_auth_invalid_marks_account_invalid(self, client, admin_auth, v1_env, tmp_path) -> None:
        backend = FakeVerifyBackend(tmp_path, code=5)
        v1_env.app.state.plane.backend = backend
        seed_account(v1_env, "acct-devin-bad", provider="devin", secret_name="sbx-acct-d")
        resp = client.post("/v1/accounts/acct-devin-bad/verify", headers=admin_auth)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "invalid"
        assert body["last_error"] == "auth_invalid"
        assert backend.terminated == ["verify-sb"]

    def test_init_failure_marks_account_invalid(self, client, admin_auth, v1_env, tmp_path) -> None:
        backend = FakeVerifyBackend(tmp_path, code=2)
        v1_env.app.state.plane.backend = backend
        seed_account(v1_env, "acct-grok-bad", provider="grok")
        resp = client.post("/v1/accounts/acct-grok-bad/verify", headers=admin_auth)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "invalid"
        assert body["last_error"] == "init_failed"
        assert backend.terminated == ["verify-sb"]

    def test_exec_error_keeps_status_and_still_terminates(
        self, client, admin_auth, v1_env, tmp_path
    ) -> None:
        backend = FakeVerifyBackend(tmp_path, exec_error=RuntimeError("exec exploded"))
        v1_env.app.state.plane.backend = backend
        seed_account(v1_env, "acct-devin-cool", provider="devin")
        v1_env.registry.mark_status("acct-devin-cool", "cooling")
        resp = client.post("/v1/accounts/acct-devin-cool/verify", headers=admin_auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "cooling"
        assert backend.terminated == ["verify-sb"]

    def test_terminate_failure_does_not_mask_result(
        self, client, admin_auth, v1_env, tmp_path
    ) -> None:
        backend = FakeVerifyBackend(
            tmp_path, code=0, terminate_error=RuntimeError("terminate exploded")
        )
        v1_env.app.state.plane.backend = backend
        seed_account(v1_env, "acct-agy-term", provider="antigravity")
        resp = client.post("/v1/accounts/acct-agy-term/verify", headers=admin_auth)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "active"
        assert backend.terminated == ["verify-sb"]

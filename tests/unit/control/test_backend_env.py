"""LocalProcessBackend env isolation (SOR-56): no unconditional os.environ."""

from __future__ import annotations

import sys

from control.backend import LocalProcessBackend, SandboxSpec


def _env_of(backend: LocalProcessBackend, spec: SandboxSpec, env: dict | None = None) -> dict:
    handle = backend.create(spec)
    try:
        proc = backend.exec(
            handle,
            [sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"],
            env=env,
        )
        out = "".join(proc.stdout)
        assert proc.wait() == 0
        import json

        return json.loads(out.strip().splitlines()[-1])
    finally:
        backend.terminate(handle)


def test_exec_does_not_inherit_parent_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "REDACTED-parent")
    monkeypatch.setenv("SBX_ACCOUNT_CREDENTIAL", "REDACTED-parent")
    backend = LocalProcessBackend()
    child = _env_of(backend, SandboxSpec())
    assert "OPENAI_API_KEY" not in child
    assert "SBX_ACCOUNT_CREDENTIAL" not in child


def test_exec_inherits_whitelist_only(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SBX_RANDOM_FLAG", "nope")
    backend = LocalProcessBackend()
    child = _env_of(backend, SandboxSpec())
    assert child.get("PATH") == "/usr/bin:/bin"
    assert "SBX_RANDOM_FLAG" not in child
    assert child["SBX_WORK"]  # setdefault to handle.root


def test_spec_env_and_call_env_overlay(monkeypatch) -> None:
    monkeypatch.delenv("LANG", raising=False)
    backend = LocalProcessBackend()
    spec = SandboxSpec(env={"A": "1", "B": "spec"})
    child = _env_of(backend, spec, env={"B": "call", "C": "3"})
    assert child["A"] == "1"
    assert child["B"] == "call"  # explicit env= overrides SandboxSpec.env
    assert child["C"] == "3"

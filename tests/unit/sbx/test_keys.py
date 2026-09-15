"""Bootstrap ``sbx_`` key handling: hash-only server side, 0600 local copy."""

from __future__ import annotations

import hashlib
import os
import stat

from sbx.keys import (
    fingerprint,
    generate_key,
    key_hash,
    load_or_create_key,
    read_key,
    resolve_api_key,
    valid_key,
)


def test_generate_format() -> None:
    token = generate_key()
    assert valid_key(token)
    assert token.startswith("sbx_")
    assert len(token) == 4 + 40


def test_fingerprint_is_hash_prefix_not_token() -> None:
    token = "sbx_" + "ab" * 20
    fp = fingerprint(token)
    assert fp == f"sha256:{hashlib.sha256(token.encode()).hexdigest()[:12]}"
    assert token not in fp


def test_load_or_create_is_atomic_0600_and_idempotent(tmp_path) -> None:
    path = tmp_path / "state" / "bootstrap.key"
    token, created = load_or_create_key(path)
    assert created and valid_key(token)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again, created2 = load_or_create_key(path)
    assert not created2 and again == token


def test_read_key_rejects_garbage(tmp_path) -> None:
    path = tmp_path / "k"
    path.write_text("not-a-key\n")
    assert read_key(path) is None
    # garbage file → treated as absent → fresh key generated
    token, created = load_or_create_key(path)
    assert created and valid_key(token)


def test_resolve_api_key_env_wins(tmp_path, monkeypatch) -> None:
    env = {"SBX_STATE_DIR": str(tmp_path), "SBX_API_KEY": "sbx_env"}
    assert resolve_api_key(env) == "sbx_env"
    env.pop("SBX_API_KEY")
    assert resolve_api_key(env) is None
    load_or_create_key(tmp_path / "bootstrap.key")
    assert resolve_api_key(env) is not None


def test_key_hash_matches_control_scheme() -> None:
    # control.api_v1.state.InMemoryApiKeyStore._hash is sha256(utf-8)
    token = generate_key()
    assert key_hash(token) == hashlib.sha256(token.encode("utf-8")).hexdigest()

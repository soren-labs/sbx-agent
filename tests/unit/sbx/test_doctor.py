"""``sbx doctor``: presence/hash-prefix reporting, never secret values."""

from __future__ import annotations

from sbx.config import BootstrapConfig, key_path
from sbx.doctor import failed, run_doctor
from sbx.keys import generate_key, load_or_create_key
from sbx.plane import SandboxInfo
from sbx_fakes import FakePlane, make_cfg, make_env, make_v1


def _healthy(tmp_path, token=None):
    env = make_env(tmp_path)
    token = token or generate_key()
    load_or_create_key(key_path(env))  # creates the state dir + file
    key_path(env).write_text(token + "\n")
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    plane.secrets["sbx-basic-auth"] = {"SBX_BASIC_USER": "sbx", "SBX_BASIC_PASSWORD": "x"}
    plane.secrets["sbx-v1-bootstrap"] = {"SBX_V1_BOOTSTRAP_KEY": token}
    for name in ("sbx-sessions", "sbx-runs", "sbx-accounts", "sbx-workflows"):
        plane.dicts[name] = {}
    plane.apps["sbx-control"] = "https://ws-test--sbx-control-fastapi-app.modal.run"
    config = BootstrapConfig(api_base_url=plane.apps["sbx-control"])
    cfg = make_cfg(tmp_path, env=env, config=config)
    return cfg, plane, env, token


def test_doctor_happy_path_never_prints_secret(tmp_path, capsys) -> None:
    cfg, plane, env, token = _healthy(tmp_path)
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    assert not failed(checks)
    blob = "\n".join(f"{c.name} {c.detail} {c.hint}" for c in checks)
    assert token not in blob  # doctor output carries no plaintext
    assert "sha256:" in blob  # only the hash prefix is shown


def test_doctor_missing_secret_fails_with_hint(tmp_path) -> None:
    cfg, plane, env, token = _healthy(tmp_path)
    del plane.secrets["sbx-codex-auth"]
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    bad = failed(checks)
    assert any(c.name == "secret:sbx-codex-auth" for c in bad)
    assert "modal secret create" in (bad[0].hint or "")


def test_doctor_unreachable_api_fails(tmp_path) -> None:
    cfg, plane, env, token = _healthy(tmp_path)
    transport, _ = make_v1(token=token, unreachable=True)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    bad = failed(checks)
    assert any(c.name == "api-reachable" for c in bad)


def test_doctor_rejected_key_fails(tmp_path) -> None:
    cfg, plane, env, token = _healthy(tmp_path)
    transport, _ = make_v1(token="sbx_different")
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    bad = failed(checks)
    assert any(c.name == "api-auth" for c in bad)


def test_doctor_no_base_url_fails(tmp_path) -> None:
    env = make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, config=BootstrapConfig())
    plane = FakePlane()
    checks = run_doctor(cfg, plane, env=env, transport=None)
    bad = failed(checks)
    assert any(c.name == "api-url" for c in bad)


def test_doctor_unauthenticated_workspace(tmp_path) -> None:
    cfg, plane, env, token = _healthy(tmp_path)
    plane.workspace_name = None
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    bad = failed(checks)
    assert any(c.name == "modal-auth" for c in bad)


def test_doctor_cleanup_capability_reported(tmp_path) -> None:
    cfg, plane, env, token = _healthy(tmp_path)
    plane.sandboxes["sb_1"] = SandboxInfo(id="sb_1", tags={"session_id": "s1"})
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    cleanup = next(c for c in checks if c.name == "cleanup")
    assert cleanup.ok and "1 live" in cleanup.detail

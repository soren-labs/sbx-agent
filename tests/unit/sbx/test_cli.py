"""CLI dispatch: entrypoints stable, errors machine-readable, exit codes."""

from __future__ import annotations

import json

from sbx.cli import main
from sbx_fakes import FakePlane, make_v1, write_state


def _args(tmp_path):
    return ["--config", str(tmp_path / "config.toml"), "--state-dir", str(tmp_path / "state")]


def test_config_json(tmp_path, capsys) -> None:
    rc = main([*_args(tmp_path), "config", "--json"], plane=FakePlane())
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["values"]["modal_app_name"]["value"] == "sbx-control"


def test_status_json(tmp_path, capsys) -> None:
    write_state(tmp_path, {"version": "0.1.0", "app_url": "https://x.modal.run"})
    rc = main([*_args(tmp_path), "status", "--json"], plane=FakePlane())
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["deployed_version"] == "0.1.0"
    assert payload["base_url"] == "https://x.modal.run"


def test_init_github_flags_and_status_reports_bridge(tmp_path, capsys) -> None:
    """SOR-133: `sbx init --github --github-secret` persists the bridge;
    `sbx status` reports the resolved state."""
    rc = main(
        [*_args(tmp_path), "init", "--github", "--github-secret", "sbx-github"],
        plane=FakePlane(),
    )
    assert rc == 0
    capsys.readouterr()
    rc = main([*_args(tmp_path), "status", "--json"], plane=FakePlane())
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["github_bridge"] == {"enabled": True, "secret_name": "sbx-github"}
    rc = main([*_args(tmp_path), "status"], plane=FakePlane())
    out = capsys.readouterr().out
    assert rc == 0 and "github:    armed (Modal Secret sbx-github)" in out


def test_status_github_bridge_off_by_default(tmp_path, capsys) -> None:
    rc = main([*_args(tmp_path), "status", "--json"], plane=FakePlane())
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["github_bridge"] == {"enabled": False, "secret_name": None}


def test_status_aggregates_providers_and_shows_live_cap(tmp_path, capsys, monkeypatch) -> None:
    """Multi-model providers collapse; live agents render against the cap."""
    monkeypatch.delenv("SBX_MAX_CONCURRENT", raising=False)
    write_state(tmp_path, {"version": "0.1.0", "app_url": "https://x.modal.run"})
    from sbx.config import key_path

    key_path({"SBX_STATE_DIR": str(tmp_path / "state")}).write_text("sbx_cli\n")
    transport, _ = make_v1(
        models=[
            {"provider": "devin", "model": "swe-2-high", "accounts_available": 1},
            {"provider": "devin", "model": "swe-2-medium", "accounts_available": 1},
        ],
        agents=[
            {"id": "a1", "status": "idle"},
            {"id": "a2", "status": "closed"},
        ],
    )
    rc = main(
        [*_args(tmp_path), "status"],
        plane=FakePlane(),
        transport=transport,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "devin: 1 account, 2 models (swe-2-high, swe-2-medium)" in out
    assert out.count("devin") == 1  # no per-model duplication
    assert "agents:    1 live (SBX_MAX_CONCURRENT unset" in out


def test_deploy_end_to_end_via_cli(tmp_path, capsys) -> None:
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    transport, _ = make_v1()
    rc = main(
        [*_args(tmp_path), "deploy", "--json"],
        plane=plane,
        transport=transport,
        sleep=lambda s: None,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["base_url"].startswith("https://")


def test_doctor_failure_exit_code_and_json_error(tmp_path, capsys) -> None:
    rc = main(
        [*_args(tmp_path), "doctor", "--json"],
        plane=FakePlane(workspace=None),
        transport=make_v1()[0],
    )
    assert rc == 1
    err = capsys.readouterr().err
    payload = json.loads(err.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == "doctor_failed"


def test_smoke_via_cli(tmp_path, capsys) -> None:
    plane = FakePlane()
    transport, _ = make_v1()
    # seed key + base_url the way deploy leaves them
    from sbx.config import BootstrapConfig, key_path, save

    save(
        BootstrapConfig(api_base_url="https://ws--sbx-control-fastapi-app.modal.run"),
        tmp_path / "config.toml",
    )
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    key_path({"SBX_STATE_DIR": str(tmp_path / "state")}).write_text("sbx_cli\n")
    rc = main(
        [*_args(tmp_path), "smoke", "--json", "--timeout", "30"],
        plane=plane,
        transport=transport,
        sleep=lambda s: None,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "FINISHED"


def test_uninstall_via_cli(tmp_path, capsys) -> None:
    plane = FakePlane()
    plane.apps["sbx-control"] = "https://ws-test--sbx-control-fastapi-app.modal.run"
    rc = main([*_args(tmp_path), "uninstall", "--json"], plane=plane)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["app_stopped"]


def test_credentials_json_clean_home(tmp_path, capsys) -> None:
    """``sbx credentials`` scans selected providers without a deployment."""
    rc = main(
        [*_args(tmp_path), "credentials", "--providers", "codex,grok", "--json"],
        plane=FakePlane(),
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    creds = {c["provider"]: c for c in payload["credentials"]}
    assert set(creds) == {"codex", "grok"}
    for entry in creds.values():
        assert entry["status"] == "not_found"
        assert entry["login"]  # official login guidance attached


def test_credentials_verify_reports_status(tmp_path, capsys, monkeypatch) -> None:
    home = tmp_path / "home"
    auth = home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"tokens": {}}')
    auth.chmod(0o600)
    # The scan resolves HOME from os.environ (conftest isolates it); point it
    # at the fixture home instead.
    monkeypatch.setenv("HOME", str(home))
    rc = main(
        [*_args(tmp_path), "credentials", "--providers", "codex", "--verify", "--json"],
        plane=FakePlane(),
        auth_check=lambda p, h: "ok",
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["credentials"][0]["status"] == "verified"


def test_error_is_machine_readable(tmp_path, capsys) -> None:
    rc = main(
        [*_args(tmp_path), "deploy", "--json"],
        plane=FakePlane(workspace=None),
        sleep=lambda s: None,
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["error"]["code"] == "modal_auth_missing"


def test_python_m_entrypoint() -> None:
    import sbx.__main__  # noqa: F401 — `python -m sbx` resolves to cli.main

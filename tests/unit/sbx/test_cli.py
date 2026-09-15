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

"""CLI dispatch: entrypoints stable, errors machine-readable, exit codes."""

from __future__ import annotations

import json

from sbx_fakes import FakePlane, make_v1, provider_row, write_state

from sbx.cli import main


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
    # SOR-217 sections: platform / runtime / accounts
    assert payload["platform"]["status"] == "unverified"  # no key on file
    assert payload["runtime"] is None  # key missing → cannot read catalog
    assert payload["accounts"] is None


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
        BootstrapConfig(
            providers=("codex",),
            api_base_url="https://ws--sbx-control-fastapi-app.modal.run",
        ),
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
    assert payload["provider"] == "codex"


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


def test_deploy_implicit_init_writes_config(tmp_path, capsys) -> None:
    """SOR-209: `sbx deploy` on a fresh checkout writes config.toml itself."""
    transport, _ = make_v1()
    rc = main(
        [*_args(tmp_path), "deploy", "--json"],
        plane=FakePlane(),
        transport=transport,
        sleep=lambda s: None,
    )
    assert rc == 0
    assert (tmp_path / "config.toml").is_file()
    payload = json.loads(capsys.readouterr().out)
    steps = {s["name"]: s for s in payload["steps"]}
    assert steps["config"]["changed"]
    assert payload["ok"]


def test_deploy_modal_auth_resumes_via_login(tmp_path, capsys) -> None:
    """SOR-209: an unauthenticated plane completes `modal token new` inside
    the same `sbx deploy` invocation instead of aborting."""
    plane = FakePlane(workspace=None)
    called: list[int] = []

    def login() -> str:
        called.append(1)
        return "ws-after-login"

    rc = main(
        [*_args(tmp_path), "deploy", "--json"],
        plane=plane,
        transport=make_v1()[0],
        sleep=lambda s: None,
        modal_login=login,
    )
    assert rc == 0
    assert called == [1]


def test_deploy_noninteractive_still_fails_fast_on_auth(tmp_path, capsys) -> None:
    """CI/non-interactive mode: no tty, no login lane — the original
    ``modal_auth_missing`` failure is preserved."""
    rc = main(
        [*_args(tmp_path), "deploy", "--json"],
        plane=FakePlane(workspace=None),
        transport=make_v1()[0],
        sleep=lambda s: None,
    )
    assert rc == 1
    err = capsys.readouterr().err
    payload = json.loads(err.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == "modal_auth_missing"


def test_open_via_cli_prints_one_time_url(tmp_path, capsys) -> None:
    """SOR-211: `sbx open` hands the browser a one-time grant — the
    long-lived sbx_ key never appears in the URL."""
    from sbx.config import BootstrapConfig, key_path, save

    save(
        BootstrapConfig(api_base_url="https://ws--sbx-control-fastapi-app.modal.run"),
        tmp_path / "config.toml",
    )
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    key_path({"SBX_STATE_DIR": str(tmp_path / "state")}).write_text("sbx_cli\n")
    rc = main(
        [*_args(tmp_path), "open", "--print"],
        plane=FakePlane(),
        transport=make_v1()[0],
    )
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "https://ws--sbx-control-fastapi-app.modal.run/#/connect?grant=sbxg_test_ticket"
    assert "sbx_cli" not in out


def test_status_reports_platform_runtime_accounts(tmp_path, capsys) -> None:
    """SOR-217: status reports the three surfaces — Platform health,
    Runtime per provider, Account summary — from /v1/providers."""
    from sbx.config import BootstrapConfig, key_path, save

    write_state(tmp_path, {"version": "0.1.0", "app_url": "https://x.modal.run"})
    save(
        BootstrapConfig(providers=("codex",), api_base_url="https://x.modal.run"),
        tmp_path / "config.toml",
    )
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    key_path({"SBX_STATE_DIR": str(tmp_path / "state")}).write_text("sbx_cli\n")
    transport, _ = make_v1(
        providers=[
            provider_row(
                "codex",
                connection_status="not_connected",
                accounts_total=0,
                accounts_available=0,
            ),
            provider_row(
                "devin",
                runtime_status="degraded",
                detail="host CLI missing",
                connection_status="degraded",
                accounts_total=1,
                accounts_available=0,
            ),
        ],
    )
    rc = main(
        [*_args(tmp_path), "status", "--json"],
        plane=FakePlane(),
        transport=transport,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["platform"]["status"] == "healthy"
    assert payload["runtime"]["codex"]["status"] == "ready"
    assert payload["runtime"]["devin"]["status"] == "degraded"
    # 0 connected providers ⇒ Platform still HEALTHY
    assert payload["accounts"] == {"connected": [], "providers": payload["accounts"]["providers"]}
    assert payload["accounts"]["providers"]["codex"]["status"] == "not_connected"

    rc = main([*_args(tmp_path), "status"], plane=FakePlane(), transport=transport)
    out = capsys.readouterr().out
    assert rc == 0
    assert "platform:  healthy" in out
    assert "codex ready" in out and "devin degraded" in out
    assert "accounts:  codex not_connected" in out


def test_auth_verify_account_ok(tmp_path, capsys) -> None:
    """`sbx auth verify <id>` probes one account server-side (SOR-217)."""
    from sbx.config import BootstrapConfig, key_path, save

    save(
        BootstrapConfig(providers=("codex",), api_base_url="https://x.modal.run"),
        tmp_path / "config.toml",
    )
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    key_path({"SBX_STATE_DIR": str(tmp_path / "state")}).write_text("sbx_cli\n")
    transport, _ = make_v1(
        accounts=[{"id": "acct-1", "provider": "codex", "status": "unknown", "last_error": None}],
        verify_status="active",
    )
    rc = main(
        [*_args(tmp_path), "auth", "verify", "acct-1", "--json"],
        plane=FakePlane(),
        transport=transport,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": True,
        "account_id": "acct-1",
        "provider": "codex",
        "status": "active",
        "last_error": None,
    }


def test_auth_verify_invalid_credential_fails(tmp_path, capsys) -> None:
    from sbx.config import BootstrapConfig, key_path, save

    save(
        BootstrapConfig(providers=("codex",), api_base_url="https://x.modal.run"),
        tmp_path / "config.toml",
    )
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    key_path({"SBX_STATE_DIR": str(tmp_path / "state")}).write_text("sbx_cli\n")
    transport, _ = make_v1(
        accounts=[{"id": "acct-1", "provider": "codex", "status": "unknown", "last_error": "401"}],
        verify_status="invalid",
    )
    rc = main(
        [*_args(tmp_path), "auth", "verify", "acct-1", "--json"],
        plane=FakePlane(),
        transport=transport,
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == "account_verify_failed"


def test_auth_verify_unknown_account_404(tmp_path, capsys) -> None:
    from sbx.config import BootstrapConfig, key_path, save

    save(
        BootstrapConfig(providers=("codex",), api_base_url="https://x.modal.run"),
        tmp_path / "config.toml",
    )
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    key_path({"SBX_STATE_DIR": str(tmp_path / "state")}).write_text("sbx_cli\n")
    transport, _ = make_v1(accounts=[])
    rc = main(
        [*_args(tmp_path), "auth", "verify", "acct-nope", "--json"],
        plane=FakePlane(),
        transport=transport,
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert payload["error"]["code"] == "account_verify_failed"
    assert "sbx status" in payload["error"]["hint"]


def test_open_no_deployment_is_actionable(tmp_path, capsys) -> None:
    rc = main([*_args(tmp_path), "open", "--json", "--print"], plane=FakePlane())
    assert rc == 1
    err = capsys.readouterr().err
    payload = json.loads(err.strip().splitlines()[-1])
    assert payload["error"]["code"] == "no_deployment"
    assert "sbx deploy" in payload["error"]["hint"]

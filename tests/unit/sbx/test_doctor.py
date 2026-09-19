"""``sbx doctor``: presence/hash-prefix reporting, never secret values."""

from __future__ import annotations

from pathlib import Path

import pytest
from sbx.config import BootstrapConfig, key_path
from sbx.deploy import deploy
from sbx.doctor import failed, run_doctor
from sbx.errors import BootstrapError
from sbx.keys import generate_key, load_or_create_key
from sbx.plane import SandboxInfo
from sbx_fakes import FakePlane, make_cfg, make_env, make_v1, write_state


def _healthy(tmp_path, token=None, providers=("codex",)):
    env = make_env(tmp_path)
    token = token or generate_key()
    load_or_create_key(key_path(env))  # creates the state dir + file
    key_path(env).write_text(token + "\n")
    plane = FakePlane()
    plane.secrets["sbx-basic-auth"] = {"SBX_BASIC_USER": "sbx", "SBX_BASIC_PASS": "x"}
    plane.secrets["sbx-v1-bootstrap"] = {"SBX_V1_BOOTSTRAP_KEY": token}
    if "codex" in providers:
        plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    for name in (
        "sbx-sessions",
        "sbx-runs",
        "sbx-accounts",
        "sbx-workflows",
        "sbx-artifacts",
        "sbx-workspaces",
    ):
        plane.dicts[name] = {}
    plane.apps["sbx-control"] = "https://ws-test--sbx-control-fastapi-app.modal.run"
    config = BootstrapConfig(api_base_url=plane.apps["sbx-control"], providers=providers)
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


def test_doctor_reads_contract_fields(tmp_path) -> None:
    """/v1 returns ``key_id`` + ``accounts_available`` — details must show them."""
    cfg, plane, env, token = _healthy(tmp_path)
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    auth = next(c for c in checks if c.name == "api-auth")
    providers = next(c for c in checks if c.name == "providers")
    assert auth.ok and "key_test" in auth.detail
    assert providers.ok and "codex: 1 account, 1 model" in providers.detail


def test_doctor_aggregates_providers_per_account(tmp_path) -> None:
    """Multi-model providers collapse to one line — no ``devin:1, devin:1``."""
    cfg, plane, env, token = _healthy(tmp_path)
    transport, _ = make_v1(
        token=token,
        models=[
            {"provider": "devin", "model": "swe-2-high", "accounts_available": 1},
            {"provider": "devin", "model": "swe-2-medium", "accounts_available": 1},
        ],
    )
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    providers = next(c for c in checks if c.name == "providers")
    assert "devin: 1 account, 2 models (swe-2-high, swe-2-medium)" in providers.detail
    assert providers.detail.count("devin") == 1


def test_doctor_provider_availability_range(tmp_path) -> None:
    """Divergent per-model availability renders as a min–max range."""
    cfg, plane, env, token = _healthy(tmp_path)
    transport, _ = make_v1(
        token=token,
        models=[
            {"provider": "devin", "model": "swe-2-high", "accounts_available": 0},
            {"provider": "devin", "model": "swe-2-medium", "accounts_available": 2},
        ],
    )
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    providers = next(c for c in checks if c.name == "providers")
    assert "devin: 0–2 accounts, 2 models" in providers.detail


def test_doctor_live_agents_reports_count_and_cap(tmp_path) -> None:
    """Idle + running agents hold slots; closed/lost ones are released."""
    cfg, plane, env, token = _healthy(tmp_path)
    transport, _ = make_v1(
        token=token,
        agents=[
            {"id": "a1", "status": "idle"},
            {"id": "a2", "status": "running"},
            {"id": "a3", "status": "closed"},  # slot released
            {"id": "a4", "status": "timed_out"},  # slot released
        ],
    )
    cfg = make_cfg(
        tmp_path,
        env=env,
        config=BootstrapConfig(api_base_url=plane.apps["sbx-control"], max_concurrent=4),
    )
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    live = next(c for c in checks if c.name == "live-agents")
    assert live.ok
    assert "2 live agents" in live.detail
    assert "cap 4" in live.detail
    assert "idle agents hold slots" in live.detail


def test_doctor_live_agents_warns_at_cap(tmp_path) -> None:
    """At the cap the check warns with concurrency_limit remediation."""
    cfg, plane, env, token = _healthy(tmp_path)
    transport, _ = make_v1(
        token=token,
        agents=[{"id": "a1", "status": "idle"}, {"id": "a2", "status": "idle"}],
    )
    cfg = make_cfg(
        tmp_path,
        env=env,
        config=BootstrapConfig(api_base_url=plane.apps["sbx-control"], max_concurrent=2),
    )
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    live = next(c for c in checks if c.name == "live-agents")
    assert not live.ok and live.warn  # advisory, not a doctor failure
    assert live not in failed(checks)
    assert "SBX_MAX_CONCURRENT" in live.detail
    assert "DELETE /v1/agents/{id}" in (live.hint or "")
    assert "deploy.max_concurrent" in (live.hint or "")


def test_doctor_live_agents_paginates(tmp_path) -> None:
    cfg, plane, env, token = _healthy(tmp_path)
    transport, _ = make_v1(
        token=token,
        agents=[
            {"id": "a1", "status": "idle"},
            {"id": "a2", "status": "running"},
            {"id": "a3", "status": "idle"},
        ],
        agents_page_size=1,
    )
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    live = next(c for c in checks if c.name == "live-agents")
    assert live.ok and "3 live agents" in live.detail


def test_doctor_falls_back_to_deploy_state_url(tmp_path) -> None:
    """Config without api.base_url still verifies the last deployed app."""
    env = make_env(tmp_path)
    token = generate_key()
    load_or_create_key(key_path(env))
    key_path(env).write_text(token + "\n")
    write_state(tmp_path, {"app_url": "https://ws--sbx-control-fastapi-app.modal.run"})
    cfg = make_cfg(tmp_path, env=env, config=BootstrapConfig())
    plane = FakePlane()
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    api_url = next(c for c in checks if c.name == "api-url")
    assert api_url.ok and "modal.run" in api_url.detail


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


def test_doctor_fails_when_account_secret_is_not_materialized(tmp_path) -> None:
    cfg, plane, env, token = _healthy(tmp_path, providers=("codex", "devin"))
    plane.dicts["sbx-accounts"]["account/devin-1"] = {
        "id": "devin-1",
        "provider": "devin",
        "secret_name": "sbx-acct-devin-1",
    }
    plane.dicts["sbx-accounts"]["credential/devin-1"] = {
        "provider": "devin",
        "files": {".local/share/devin/credentials.toml": "REDACTED"},
    }
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    bad = failed(checks)
    account = next(c for c in bad if c.name == "account-secrets")
    assert "sbx-acct-devin-1" in account.detail
    assert "sbx deploy" in (account.hint or "")


def test_doctor_reports_materialized_account_secrets(tmp_path) -> None:
    cfg, plane, env, token = _healthy(tmp_path, providers=("codex", "devin"))
    plane.dicts["sbx-accounts"]["account/devin-1"] = {
        "id": "devin-1",
        "provider": "devin",
        "secret_name": "sbx-acct-devin-1",
    }
    plane.secrets["sbx-acct-devin-1"] = {"SBX_ACCOUNT_CREDENTIAL": "REDACTED"}
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    account = next(c for c in checks if c.name == "account-secrets")
    assert account.ok and "1 referenced" in account.detail


def test_doctor_codex_secret_not_required_for_non_codex_deploy(tmp_path) -> None:
    """An unselected provider's credential must not block onboarding: a
    devin-only deploy never requires ``sbx-codex-auth`` (SOR-115)."""
    cfg, plane, env, token = _healthy(tmp_path)
    del plane.secrets["sbx-codex-auth"]
    cfg = make_cfg(
        tmp_path,
        env=env,
        config=BootstrapConfig(providers=("devin",), api_base_url=cfg.config.api_base_url),
    )
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    names = [c.name for c in checks]
    assert "secret:sbx-codex-auth" not in names
    assert not failed(checks)


def test_doctor_scans_local_credentials_advisory(tmp_path) -> None:
    cfg, plane, env, token = _healthy(tmp_path)
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    cred = next(c for c in checks if c.name == "cred:codex")
    assert not cred.ok and cred.warn  # clean HOME: guidance, not a failure
    assert "codex login" in (cred.hint or "")
    assert not failed(checks)


def test_doctor_verify_runs_provider_auth_check(tmp_path) -> None:
    cfg, plane, env, token = _healthy(tmp_path)
    home = Path(env["HOME"])
    auth = home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{}")
    auth.chmod(0o600)
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport, auth_check=lambda p, h: "ok")
    cred = next(c for c in checks if c.name == "cred:codex")
    assert cred.ok and "auth check passed" in cred.detail


def test_doctor_devin_only_never_requires_codex_secret(tmp_path) -> None:
    """SOR-116 gate: providers=[devin] → no secret:sbx-codex-auth check."""
    cfg, plane, env, token = _healthy(tmp_path, providers=("devin",))
    assert "sbx-codex-auth" not in plane.secrets
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    assert not failed(checks)
    assert all(c.name != "secret:sbx-codex-auth" for c in checks)
    provider_check = next(c for c in checks if c.name == "provider-config")
    assert provider_check.ok and "devin" in provider_check.detail


def test_doctor_empty_providers_fail_provider_config(tmp_path) -> None:
    cfg, plane, env, token = _healthy(tmp_path, providers=())
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    bad = failed(checks)
    assert any(c.name == "provider-config" for c in bad)


def test_doctor_ignores_disabled_provider_account_secret(tmp_path) -> None:
    """A devin account's missing Secret is no codex-only doctor failure."""
    cfg, plane, env, token = _healthy(tmp_path)  # providers=("codex",)
    plane.dicts["sbx-accounts"]["account/devin-1"] = {
        "id": "devin-1",
        "provider": "devin",
        "secret_name": "sbx-acct-devin-1",
    }
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    assert not failed(checks)
    account = next(c for c in checks if c.name == "account-secrets")
    assert account.ok


def test_doctor_and_deploy_agree_on_provider_prereqs(tmp_path) -> None:
    """Same resolved config drives both commands: providers=[codex,devin]
    with no sbx-codex-auth → doctor flags the Secret deploy fails on."""
    cfg, plane, env, token = _healthy(tmp_path, providers=("codex", "devin"))
    del plane.secrets["sbx-codex-auth"]
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    assert any(c.name == "secret:sbx-codex-auth" for c in failed(checks))

    with pytest.raises(BootstrapError) as exc:
        deploy(cfg, plane, env=env, transport=transport, sleep=lambda s: None)
    assert exc.value.code == "secret_missing"
    assert plane.secret_create_calls == 0  # fail-before-write preserved


def test_doctor_reports_github_bridge_without_secret(tmp_path) -> None:
    """SOR-117: an ambient GH_TOKEN is reported by *name* — advisory warn,
    opt-in hint, and the token value never reaches doctor output."""
    cfg, plane, env, token = _healthy(tmp_path)
    env = {**env, "GH_TOKEN": "REDACTED_GITHUB"}
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    gh = next(c for c in checks if c.name == "github")
    assert gh not in failed(checks)
    assert not gh.ok and gh.warn
    assert "GH_TOKEN" in gh.detail
    assert "SBX_GITHUB_EPHEMERAL" in (gh.hint or "")
    blob = "\n".join(f"{c.name} {c.detail} {c.hint}" for c in checks)
    assert "REDACTED_GITHUB" not in blob


def test_doctor_checks_named_github_secret(tmp_path) -> None:
    """SOR-133: an armed bridge's configured Secret name is presence-checked
    like the other prerequisites — a missing one fails doctor exactly as it
    fails ``sbx deploy``."""
    cfg, plane, env, token = _healthy(tmp_path)
    cfg = make_cfg(
        tmp_path,
        env=env,
        config=BootstrapConfig(
            api_base_url=plane.apps["sbx-control"],
            github_ephemeral=True,
            github_secret_name="sbx-github",
        ),
    )
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    gh_secret = next(c for c in checks if c.name == "secret:sbx-github")
    assert gh_secret in failed(checks)

    plane.secrets["sbx-github"] = {"GH_TOKEN": "REDACTED_GITHUB"}
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    gh_secret = next(c for c in checks if c.name == "secret:sbx-github")
    assert gh_secret.ok
    gh = next(c for c in checks if c.name == "github")
    assert gh.ok and "sbx-github" in gh.detail
    blob = "\n".join(f"{c.name} {c.detail} {c.hint}" for c in checks)
    assert "REDACTED_GITHUB" not in blob


def test_doctor_github_env_disarms_persisted_bridge(tmp_path) -> None:
    """SBX_GITHUB_EPHEMERAL=0 disarms a file-persisted bridge: the advisory
    reports armed-config-vs-off and the named Secret is not required."""
    cfg, plane, env, token = _healthy(tmp_path)
    save = BootstrapConfig(
        api_base_url=cfg.config.api_base_url,
        github_ephemeral=True,
        github_secret_name="sbx-github",
    )
    env = {**env, "SBX_GITHUB_EPHEMERAL": "0"}
    cfg = make_cfg(tmp_path, env=env, config=save)
    transport, _ = make_v1(token=token)
    checks = run_doctor(cfg, plane, env=env, transport=transport)
    gh = next(c for c in checks if c.name == "github")
    assert not gh.ok and gh.warn
    assert "configured but the bridge is off" in gh.detail
    assert all(c.name != "secret:sbx-github" for c in checks)

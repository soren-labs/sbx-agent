"""``sbx deploy`` local-assisted lane (SOR-212/SOR-215).

Host-binary providers (agy/grok) ship their CLI from the build host — a
missing or wrong-version binary degrades the provider instead of failing
the Platform deploy, and the resolution lock carries the evidence. Deploy
also writes ``runtime/<provider>`` records into the ``sbx-runtime`` Dict
for ``/v1/providers``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sbx_fakes import FakePlane, make_cfg, make_env, make_v1

from sbx.config import BootstrapConfig, state_dir
from sbx.deploy import deploy, read_deploy_state
from sbx.errors import BootstrapError


def _deploy(tmp_path, plane, *, env=None, config=None, **kwargs):
    env = env or make_env(tmp_path)
    cfg = make_cfg(tmp_path, env=env, config=config)
    transport, http = make_v1()
    report = deploy(
        cfg,
        plane,
        env=env,
        transport=transport,
        sleep=lambda s: None,
        **kwargs,
    )
    return report, env, http


def _codex_agy() -> BootstrapConfig:
    return BootstrapConfig(providers=("codex", "antigravity"))


def _fake_cli(tmp_path: Path, name: str, version: str) -> Path:
    """A build-host CLI stub reporting ``--version`` as ``<name> <version>``."""
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\necho {name} {version}\n")
    path.chmod(0o755)
    return path


def test_missing_host_cli_degrades_not_blocks(tmp_path) -> None:
    """No agy on the build host: deploy completes, the provider is marked
    degraded, its image is skipped, and the lock records ``unresolved``."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    report, env, _ = _deploy(tmp_path, plane, config=_codex_agy())

    assert plane.image_calls == ["codex"]
    step = next(s for s in report.steps if s.name == "image:antigravity")
    assert step.changed is False
    assert "not built" in step.detail and "agy" in step.detail

    runtime = plane.dicts["sbx-runtime"]
    assert runtime["runtime/codex"]["status"] == "ready"
    assert runtime["runtime/codex"]["image"] == "sbx-runtime"
    agy = runtime["runtime/antigravity"]
    assert agy["status"] == "degraded"
    assert agy["image"] == "sbx-runtime-antigravity"
    assert "agy CLI not found" in agy["detail"]
    assert agy["schema"] == "sbx-runtime/provider@1"

    state = read_deploy_state(env)
    assert state["runtime"]["antigravity"]["status"] == "degraded"
    assert state["runtime"]["codex"]["status"] == "ready"
    providers = state["cli_versions"]["providers"]
    assert providers["antigravity"]["source"] == "unresolved"
    assert providers["antigravity"]["version"] is None


def test_latest_request_without_host_cli_never_probes(tmp_path) -> None:
    """``latest`` for a host-binary provider resolves via the host binary;
    when absent it must degrade — the host probe is never even invoked."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    env = make_env(tmp_path, {"SBX_AGY_VERSION": "latest"})

    def _probe(provider, _env):
        raise AssertionError(f"host probe ran for {provider}")

    report, env, _ = _deploy(tmp_path, plane, env=env, config=_codex_agy(), host_probe=_probe)
    assert plane.image_calls == ["codex"]
    state = read_deploy_state(env)
    providers = state["cli_versions"]["providers"]
    assert providers["antigravity"]["requested"] == "latest"
    assert providers["antigravity"]["source"] == "unresolved"
    assert "not found" in providers["antigravity"]["evidence"]["detail"]


def test_present_host_cli_builds_and_reports_ready(tmp_path) -> None:
    """A pinned host binary satisfying the pin builds the image and writes a
    ``ready`` record."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    agy = _fake_cli(tmp_path, "agy", "1.2.3")
    env = make_env(tmp_path, {"SBX_AGY_BIN": str(agy)})

    report, env, _ = _deploy(tmp_path, plane, env=env, config=_codex_agy())
    assert plane.image_calls == ["codex", "antigravity"]
    assert plane.dicts["sbx-runtime"]["runtime/antigravity"]["status"] == "ready"


def test_latest_request_resolves_through_host_cli(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``latest`` + a working host binary: the resolution lane stays intact —
    the host ``--version`` output becomes the frozen version."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    agy = _fake_cli(tmp_path, "agy", "9.9.9")
    # ``runtime.versions._probe_host_version`` reads the ambient env for the
    # bin path — set both seams.
    monkeypatch.setenv("SBX_AGY_BIN", str(agy))
    env = make_env(tmp_path, {"SBX_AGY_BIN": str(agy), "SBX_AGY_VERSION": "latest"})

    report, env, _ = _deploy(tmp_path, plane, env=env, config=_codex_agy())
    assert plane.image_calls == ["codex", "antigravity"]
    assert report.cli_versions["antigravity"] == "9.9.9"
    assert plane.image_specs["antigravity"].agy_version == "9.9.9"
    state = read_deploy_state(env)
    assert state["cli_versions"]["providers"]["antigravity"]["source"] == "host-binary"
    assert state["runtime"]["antigravity"]["status"] == "ready"


def test_wrong_version_host_cli_degrades_not_blocks(tmp_path) -> None:
    """A host CLI reporting a different version than the pin degrades the
    provider instead of failing the deploy."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    agy = _fake_cli(tmp_path, "agy", "0.0.1")
    env = make_env(tmp_path, {"SBX_AGY_BIN": str(agy)})

    report, env, _ = _deploy(tmp_path, plane, env=env, config=_codex_agy())
    assert plane.image_calls == ["codex"]
    agy_rec = plane.dicts["sbx-runtime"]["runtime/antigravity"]
    assert agy_rec["status"] == "degraded"
    assert "pins 1.2.3" in agy_rec["detail"]


def test_reproducible_provider_still_blocks_on_failure(tmp_path) -> None:
    """The lane is local-assisted only: reproducible providers keep
    hard-failing — a failed devin ``latest`` resolution aborts the deploy."""
    plane = FakePlane()

    def _fetch(url: str):
        from runtime.versions import VersionResolutionError

        raise VersionResolutionError("devin", "upstream manifest unreachable")

    env = make_env(tmp_path, {"SBX_DEVIN_VERSION": "latest"})
    with pytest.raises(BootstrapError) as excinfo:
        _deploy(
            tmp_path,
            plane,
            env=env,
            config=BootstrapConfig(providers=("devin",)),
            fetch=_fetch,
        )
    assert excinfo.value.code == "version_resolution_failed"


def test_runtime_evidence_write_failure_never_blocks(tmp_path) -> None:
    """A Dict write failure must not fail the deploy — evidence is
    best-effort and the next run rewrites it."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    plane.fail_on.add("dict_put")
    report, env, _ = _deploy(tmp_path, plane, config=_codex_agy())
    assert plane.image_calls == ["codex"]
    # deploy.json still records the evidence for doctor/local readers
    state = read_deploy_state(env)
    assert state["runtime"]["codex"]["status"] == "ready"


def test_runtime_dict_ensured_and_runtime_section_in_state(tmp_path) -> None:
    """Baseline: a codex-only deploy creates ``sbx-runtime`` and writes the
    codex ready record."""
    plane = FakePlane()
    plane.secrets["sbx-codex-auth"] = {"CODEX_AUTH_JSON": "REDACTED"}
    report, env, _ = _deploy(tmp_path, plane, config=BootstrapConfig(providers=("codex",)))
    assert "sbx-runtime" in plane.dicts
    codex = plane.dicts["sbx-runtime"]["runtime/codex"]
    assert codex["status"] == "ready"
    assert codex["version"] is not None
    lock = json.loads((state_dir(env) / "cli-versions.json").read_text())
    assert lock["providers"]["codex"]["version"] == codex["version"]


def test_deploy_env_forwards_runtime_dict_name(tmp_path) -> None:
    """The remote app reads ``SBX_RUNTIME_DICT`` for its runtime store —
    the resolved name must reach the deploy env."""
    cfg = BootstrapConfig(providers=("codex",))
    env = cfg.deploy_env()
    assert env["SBX_RUNTIME_DICT"] == "sbx-runtime"


def test_remote_env_keys_forward_runtime_dict() -> None:
    from control.config import remote_env_overlay

    overlay = remote_env_overlay({"SBX_RUNTIME_DICT": "custom-runtime"})
    assert overlay["SBX_RUNTIME_DICT"] == "custom-runtime"


def test_old_deploy_state_without_runtime_reads_clean(tmp_path) -> None:
    """Backcompat: a pre-SOR-212 deploy.json has no ``runtime`` section —
    readers must tolerate its absence."""
    from sbx.deploy import read_deploy_state

    env = make_env(tmp_path)
    path = state_dir(env) / "deploy.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": "0.1.0", "app": "sbx-control"}))
    state = read_deploy_state(env)
    assert state["app"] == "sbx-control"
    assert "runtime" not in state  # tolerated: no KeyError, no crash

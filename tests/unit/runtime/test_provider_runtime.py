"""``ProviderRuntimeSpec`` registry — the single declarative contract
(SOR-212/SOR-215).

These tests pin the spec's parity with the truth that predates it — the
image builders, credential-file map, onboarding probe argv and default
models — and the no-credential-in-image invariant: runtime env / install
metadata never carry credential paths or material.
"""

from __future__ import annotations

import json

import pytest
from runtime.provider_runtime import (
    PROVIDER_RUNTIME_IDS,
    PROVIDER_RUNTIME_SPECS,
    provider_runtime_specs,
    runtime_spec,
    spec_or_none,
)

CONTRACT_PROVIDERS = {"codex", "antigravity", "grok", "opencode", "devin"}


def test_registry_covers_the_contract_provider_set() -> None:
    assert set(PROVIDER_RUNTIME_IDS) == CONTRACT_PROVIDERS
    assert {s.provider for s in PROVIDER_RUNTIME_SPECS} == CONTRACT_PROVIDERS
    assert spec_or_none("claude") is None
    with pytest.raises(KeyError):
        runtime_spec("claude")


def test_spec_parity_with_image_meta_and_builders() -> None:
    from runtime.image import (
        IMAGE_BUILDERS,
        PROVIDER_CREDENTIAL_FILES,
        _provider_cli_meta,
        image_for,
        load_packages,
    )

    spec = load_packages()
    assert set(IMAGE_BUILDERS) == CONTRACT_PROVIDERS
    for rspec in provider_runtime_specs():
        meta = _provider_cli_meta(rspec.provider, spec)
        assert meta["cli"] == rspec.cli
        assert meta["cli_path"] == rspec.cli_path
        assert meta["version"] == rspec.version_of(spec)
        assert image_for(rspec.provider) == rspec.image_name
        assert meta["install"]["kind"] == rspec.install_kind
        if rspec.local_assisted:
            assert meta["install"]["env"] == rspec.host_bin_env
        assert tuple(PROVIDER_CREDENTIAL_FILES[rspec.provider]) == rspec.credential_files


def test_spec_parity_with_onboarding_probes() -> None:
    """Auth/model probe argv mirror SOR-206/207 truth — the spec must not
    drift from what onboarding actually invokes."""
    from control.onboarding import (
        PROVIDER_AUTH_CHECKS,
        PROVIDER_DESCRIPTORS,
        PROVIDER_MODEL_CHECKS,
    )

    descriptors = {d.provider: d for d in PROVIDER_DESCRIPTORS}
    for rspec in provider_runtime_specs():
        assert PROVIDER_AUTH_CHECKS[rspec.provider] == rspec.auth_argv
        assert PROVIDER_MODEL_CHECKS[rspec.provider] == rspec.models_argv
        descriptor = descriptors[rspec.provider]
        assert descriptor.support == rspec.support
        assert tuple(descriptor.credential_files) == rspec.credential_files


def test_spec_parity_with_default_models() -> None:
    from control.api_v1.bootstrap import PROVIDER_DEFAULT_MODELS

    for rspec in provider_runtime_specs():
        assert tuple(PROVIDER_DEFAULT_MODELS[rspec.provider]) == rspec.default_models


def test_reproducible_vs_local_assisted_lanes() -> None:
    lanes = {s.provider: s.local_assisted for s in provider_runtime_specs()}
    assert lanes == {
        "codex": False,
        "devin": False,
        "opencode": False,
        "antigravity": True,
        "grok": True,
    }
    for rspec in provider_runtime_specs():
        assert rspec.reproducible is (not rspec.local_assisted)


def test_distribution_completeness() -> None:
    """Every lane declares its distribution inputs: npm package, bundle
    checksums, or host-binary override + default path."""
    for rspec in provider_runtime_specs():
        if rspec.install_kind == "npm":
            assert rspec.npm_field
        if rspec.install_kind == "bundle":
            assert rspec.checksum_fields
        if rspec.install_kind == "host-binary":
            assert rspec.host_bin_env and rspec.host_bin_default.startswith("~/")


def test_runtime_env_shapes() -> None:
    for rspec in provider_runtime_specs():
        env = rspec.runtime_env("/work")
        # runtime env pins HOME/XDG locations only — never credentials.
        assert set(env) <= {
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
        }
        for value in env.values():
            assert "auth" not in value and "credential" not in value
    assert runtime_spec("codex").runtime_env() == {}
    assert runtime_spec("grok").runtime_env() == {"HOME": "/work/home"}
    assert "XDG_DATA_HOME" in runtime_spec("devin").runtime_env()
    assert "XDG_DATA_HOME" in runtime_spec("opencode").runtime_env()


def test_no_credential_material_in_manifest_surface() -> None:
    """The image manifest must never carry credential *contents* — credential
    relpaths appear only under ``credential_files``."""
    from runtime.image import image_manifest

    manifest = image_manifest()
    for provider, meta in manifest["providers"].items():
        env_and_install = json.dumps(
            {"env": meta["env"], "install": meta["install"]}, sort_keys=True
        )
        assert "auth.json" not in env_and_install
        assert "credentials.toml" not in env_and_install
        assert "oauth-token" not in env_and_install
        assert set(meta["credential_files"]) == set(runtime_spec(provider).credential_files)


def test_host_bin_resolution(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    rspec = runtime_spec("antigravity")
    # Isolated HOME — the well-known path does not exist → None (not raise).
    monkeypatch.delenv("SBX_AGY_BIN", raising=False)
    assert rspec.host_bin({}) is None
    # Default lookup is the well-known ``~/.local/bin/agy``.
    assert rspec.host_bin_path({}) == tmp_path / "home/.local/bin/agy"

    fake = tmp_path / "agy"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    assert rspec.host_bin({"SBX_AGY_BIN": str(fake)}) == fake.resolve()
    # Ambient os.environ works when the mapping does not carry the key.
    monkeypatch.setenv("SBX_AGY_BIN", str(fake))
    assert rspec.host_bin({}) == fake.resolve()


def test_host_assist_problem_missing_bin() -> None:
    from runtime.image import load_packages

    rspec = runtime_spec("grok")
    problem = rspec.host_assist_problem(load_packages(), env={})
    assert problem is not None
    assert "grok CLI not found" in problem
    assert "SBX_GROK_BIN" in problem


def test_host_assist_problem_version_mismatch(tmp_path) -> None:
    from runtime.image import load_packages

    fake = tmp_path / "agy"
    fake.write_text("#!/bin/sh\necho agy 0.0.1\n")
    fake.chmod(0o755)
    rspec = runtime_spec("antigravity")
    problem = rspec.host_assist_problem(load_packages(), env={"SBX_AGY_BIN": str(fake)})
    assert problem is not None
    assert "pins 1.2.3" in problem


def test_host_assist_problem_satisfiable(tmp_path) -> None:
    from runtime.image import load_packages

    fake = tmp_path / "agy"
    fake.write_text("#!/bin/sh\necho agy 1.2.3\n")
    fake.chmod(0o755)
    rspec = runtime_spec("antigravity")
    assert rspec.host_assist_problem(load_packages(), env={"SBX_AGY_BIN": str(fake)}) is None


def test_host_assist_problem_version_probe_failure(tmp_path) -> None:
    """A host CLI that cannot answer ``--version`` degrades with the probe
    error — never raises."""
    from runtime.image import load_packages

    fake = tmp_path / "agy"
    fake.write_text("#!/bin/sh\nexit 7\n")
    fake.chmod(0o755)
    rspec = runtime_spec("antigravity")
    problem = rspec.host_assist_problem(load_packages(), env={"SBX_AGY_BIN": str(fake)})
    assert problem is not None
    assert "--version" in problem


def test_host_assist_problem_latest_is_satisfied_by_presence(tmp_path) -> None:
    """For a ``latest`` request the host binary IS the version source —
    presence satisfies the lane."""
    import dataclasses

    from runtime.image import load_packages

    fake = tmp_path / "agy"
    fake.write_text("#!/bin/sh\necho whatever\n")
    fake.chmod(0o755)
    spec = dataclasses.replace(load_packages(), agy_version="latest")
    rspec = runtime_spec("antigravity")
    assert rspec.host_assist_problem(spec, env={"SBX_AGY_BIN": str(fake)}) is None


def test_reproducible_lane_has_no_host_assist() -> None:
    from runtime.image import load_packages

    assert runtime_spec("codex").host_assist_problem(load_packages()) is None
    assert runtime_spec("devin").host_assist_problem(load_packages()) is None


def test_version_of_reads_package_spec() -> None:
    from runtime.image import load_packages

    spec = load_packages()
    for rspec in provider_runtime_specs():
        assert rspec.version_of(spec) == getattr(spec, rspec.spec_field)


def test_version_env_names_match_versions_module() -> None:
    from runtime.versions import _SPEC_FIELDS, VERSION_ENVS

    for rspec in provider_runtime_specs():
        assert _SPEC_FIELDS[rspec.provider] == rspec.spec_field
        assert VERSION_ENVS[rspec.provider] == f"SBX_{rspec.cli.upper()}_VERSION"


def test_probe_bin_env_names_match_onboarding() -> None:
    from control.onboarding import _PROVIDER_BINS

    for rspec in provider_runtime_specs():
        env_name, cli = _PROVIDER_BINS[rspec.provider]
        assert env_name == rspec.bin_env
        assert cli == rspec.cli

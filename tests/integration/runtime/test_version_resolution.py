"""SOR-175: build/deploy-time provider CLI version resolution + freeze.

``runtime.versions.resolve_versions`` turns a ``latest`` request in
``runtime/packages.txt`` (or a ``SBX_*_VERSION`` override) into one concrete
version per provider, resolved **once** on the build host — npm ``latest``
dist-tags, the Devin ``current/manifest.json`` pointer + its sha256
checksums, or the host binary's own ``--version`` — then freezes the set to
a lock file. Replaying the lock (``--versions-lock`` / ``SBX_VERSIONS_LOCK``)
reproduces a previous deployment verbatim: the rollback lane.

Every test injects ``fetch``/``host_probe`` fakes — nothing touches the
network or real provider binaries, per AGENTS.md §3.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from runtime.image import (
    image_manifest,
    load_packages,
    render_dockerfile_local,
)
from runtime.image import (
    main as image_main,
)
from runtime.versions import (
    LOCK_SCHEMA,
    VersionResolutionError,
    lock_out_path_for,
    read_lock,
    resolve_versions,
    write_lock,
)

ENV: dict[str, str] = {}


def _fail_fetch(url: str) -> Mapping[str, Any]:
    raise AssertionError(f"unexpected fetch: {url}")


def _fail_probe(provider: str, env: Mapping[str, str]) -> tuple[str, str]:
    raise AssertionError(f"unexpected host probe: {provider}")


def _npm_fetch(versions: Mapping[str, str], calls: list[str]):
    """Fake npm ``{registry}/<pkg>/latest`` endpoint."""

    def fetch(url: str) -> Mapping[str, Any]:
        calls.append(url)
        for package, version in versions.items():
            if url.endswith(f"/{package}/latest") or f"%2F{package.split('/')[-1]}" in url:
                return {"version": version, "name": package}
        raise AssertionError(f"unexpected npm fetch: {url}")

    return fetch


def _devin_manifest(version: str, sha_x: str = "a" * 64, sha_a: str = "b" * 64):
    return {
        "version": version,
        "platforms": {
            "x86_64-unknown-linux": {"url": "u-x", "sha256": sha_x},
            "aarch64-unknown-linux": {"url": "u-a", "sha256": sha_a},
        },
    }


# ---------------------------------------------------------------------- pins


def test_pinned_spec_resolves_without_any_probe() -> None:
    """All-pinned packages.txt is the zero-network path: a fetch or host
    probe here would be a regression."""
    spec = load_packages()
    resolved = resolve_versions(spec, ENV, fetch=_fail_fetch, host_probe=_fail_probe)
    assert resolved.spec == spec
    assert set(resolved.entries) == {"codex", "devin", "antigravity", "grok", "opencode"}
    assert resolved.entries["codex"].source == "pin"
    assert resolved.entries["devin"].source == "pin"
    assert resolved.entries["devin"].evidence["sha256"]["x86_64-unknown-linux"] == (
        spec.devin_sha256_x86_64
    )
    assert resolved.replayed_from is None
    assert resolved.cli_versions()["codex"] == spec.codex_version


def test_env_override_beats_packages_txt_pin() -> None:
    spec = load_packages()
    env = {"SBX_CODEX_VERSION": "9.9.9"}
    resolved = resolve_versions(
        spec, env, providers={"codex"}, fetch=_fail_fetch, host_probe=_fail_probe
    )
    assert resolved.spec.codex_version == "9.9.9"
    entry = resolved.entries["codex"]
    assert entry.requested == "9.9.9" and entry.source == "env-override"
    # Only the selected provider resolved; the rest keep raw values.
    assert resolved.spec.devin_version == spec.devin_version
    assert "devin" not in resolved.entries


def test_unknown_provider_rejected() -> None:
    with pytest.raises(VersionResolutionError):
        resolve_versions(
            load_packages(), ENV, providers={"bogus"}, fetch=_fail_fetch, host_probe=_fail_probe
        )


# -------------------------------------------------------------------- latest


def test_codex_latest_resolves_via_npm_dist_tag() -> None:
    spec = replace(load_packages(), codex_version="latest")
    calls: list[str] = []
    fetch = _npm_fetch({"openai/codex": "0.160.0"}, calls)
    resolved = resolve_versions(spec, ENV, providers={"codex"}, fetch=fetch, host_probe=_fail_probe)
    assert resolved.spec.codex_version == "0.160.0"
    assert resolved.spec.codex_npm_spec == "@openai/codex@0.160.0"
    entry = resolved.entries["codex"]
    assert entry.requested == "latest" and entry.source == "npm-dist-tag"
    assert len(calls) == 1
    assert calls[0].endswith("/%40openai%2Fcodex/latest")


def test_opencode_latest_via_env_override() -> None:
    spec = load_packages()
    env = {"SBX_OPENCODE_VERSION": "latest"}
    calls: list[str] = []
    fetch = _npm_fetch({"opencode-ai": "2.0.0"}, calls)
    resolved = resolve_versions(
        spec, env, providers={"opencode"}, fetch=fetch, host_probe=_fail_probe
    )
    assert resolved.spec.opencode_version == "2.0.0"
    assert resolved.entries["opencode"].source == "npm-dist-tag"


def test_npm_registry_env_override_is_used() -> None:
    spec = replace(load_packages(), codex_version="latest")
    env = {"SBX_NPM_REGISTRY": "https://npm.internal.example"}
    calls: list[str] = []
    fetch = _npm_fetch({"openai/codex": "0.160.0"}, calls)
    resolve_versions(spec, env, providers={"codex"}, fetch=fetch, host_probe=_fail_probe)
    assert calls[0].startswith("https://npm.internal.example/")


def test_devin_latest_resolves_via_current_manifest() -> None:
    spec = replace(load_packages(), devin_version="latest")
    calls: list[str] = []
    manifest = _devin_manifest("3000.99.0", "c" * 64, "d" * 64)

    def fetch(url: str) -> Mapping[str, Any]:
        calls.append(url)
        assert url.endswith("/current/manifest.json")
        return manifest

    resolved = resolve_versions(spec, ENV, providers={"devin"}, fetch=fetch, host_probe=_fail_probe)
    assert resolved.spec.devin_version == "3000.99.0"
    # The checksums travel with the version — the image's sha256 gate keeps
    # verifying the same concrete artifact the build host resolved.
    assert resolved.spec.devin_sha256_x86_64 == "c" * 64
    assert resolved.spec.devin_sha256_aarch64 == "d" * 64
    entry = resolved.entries["devin"]
    assert entry.source == "devin-manifest"
    assert entry.evidence["sha256"]["aarch64-unknown-linux"] == "d" * 64


def test_devin_env_pin_fetches_versioned_manifest_for_checksums() -> None:
    spec = load_packages()
    env = {"SBX_DEVIN_VERSION": "3000.55.0"}
    calls: list[str] = []

    def fetch(url: str) -> Mapping[str, Any]:
        calls.append(url)
        return _devin_manifest("3000.55.0", "e" * 64, "f" * 64)

    resolved = resolve_versions(spec, env, providers={"devin"}, fetch=fetch, host_probe=_fail_probe)
    assert resolved.spec.devin_version == "3000.55.0"
    assert calls == [f"{spec.devin_base_url}/3000.55.0/manifest.json"]
    assert resolved.spec.devin_sha256_x86_64 == "e" * 64


def test_devin_env_pin_with_explicit_checksums_needs_no_fetch() -> None:
    spec = load_packages()
    env = {
        "SBX_DEVIN_VERSION": "3000.55.0",
        "SBX_DEVIN_SHA256_X86_64": "1" * 64,
        "SBX_DEVIN_SHA256_AARCH64": "2" * 64,
    }
    resolved = resolve_versions(
        spec, env, providers={"devin"}, fetch=_fail_fetch, host_probe=_fail_probe
    )
    assert resolved.spec.devin_sha256_x86_64 == "1" * 64
    assert resolved.spec.devin_sha256_aarch64 == "2" * 64
    assert resolved.entries["devin"].source == "env-override"


def test_devin_partial_checksum_override_is_rejected() -> None:
    env = {"SBX_DEVIN_VERSION": "3000.55.0", "SBX_DEVIN_SHA256_X86_64": "1" * 64}
    with pytest.raises(VersionResolutionError) as exc:
        resolve_versions(
            load_packages(), env, providers={"devin"}, fetch=_fail_fetch, host_probe=_fail_probe
        )
    assert exc.value.provider == "devin"
    assert "SBX_DEVIN_SHA256_AARCH64" in (exc.value.hint or "")


def test_devin_manifest_missing_platform_sha_is_rejected() -> None:
    spec = replace(load_packages(), devin_version="latest")
    manifest = {
        "version": "3000.99.0",
        "platforms": {"x86_64-unknown-linux": {"sha256": "c" * 64}},
    }
    with pytest.raises(VersionResolutionError) as exc:
        resolve_versions(
            spec,
            ENV,
            providers={"devin"},
            fetch=lambda url: manifest,
            host_probe=_fail_probe,
        )
    assert exc.value.provider == "devin"
    assert "aarch64" in exc.value.detail


def test_host_binary_providers_resolve_latest_via_host_probe() -> None:
    spec = replace(load_packages(), agy_version="latest", grok_version="latest")
    probes: list[str] = []

    def host_probe(provider: str, env: Mapping[str, str]) -> tuple[str, str]:
        probes.append(provider)
        return {"antigravity": ("1.9.9", "/host/bin/agy"), "grok": ("2.0.1", "/host/bin/grok")}[
            provider
        ]

    resolved = resolve_versions(
        spec, ENV, providers={"antigravity", "grok"}, fetch=_fail_fetch, host_probe=host_probe
    )
    assert resolved.spec.agy_version == "1.9.9"
    assert resolved.spec.grok_version == "2.0.1"
    assert resolved.entries["antigravity"].source == "host-binary"
    assert resolved.entries["antigravity"].evidence["bin"] == "/host/bin/agy"
    assert sorted(probes) == ["antigravity", "grok"]


def test_npm_fetch_failure_is_actionable() -> None:
    spec = replace(load_packages(), codex_version="latest")

    def fetch(url: str) -> Mapping[str, Any]:
        raise VersionResolutionError("", f"GET {url} failed: boom")

    with pytest.raises(VersionResolutionError) as exc:
        resolve_versions(spec, ENV, providers={"codex"}, fetch=fetch, host_probe=_fail_probe)
    assert exc.value.provider == "codex"
    assert "registry" in (exc.value.hint or "")


# --------------------------------------------------------------- lock + replay


def test_lock_roundtrip_and_replay_freezes_versions(tmp_path: Path) -> None:
    spec = replace(load_packages(), codex_version="latest")
    fetch = _npm_fetch({"openai/codex": "0.160.0"}, [])
    resolved = resolve_versions(spec, ENV, providers={"codex"}, fetch=fetch, host_probe=_fail_probe)
    lock_path = write_lock(resolved, tmp_path / "cli-versions.json")
    assert json.loads(lock_path.read_text())["schema"] == LOCK_SCHEMA

    # Replay: no fetch, no probe — the lock wins over the "latest" request
    # and over a different packages.txt pin alike.
    replayed = resolve_versions(
        spec, ENV, providers={"codex"}, fetch=_fail_fetch, host_probe=_fail_probe, lock=lock_path
    )
    assert replayed.spec.codex_version == "0.160.0"
    assert replayed.entries["codex"].source == "lock"
    assert replayed.replayed_from == str(lock_path)


def test_lock_replay_overrides_a_changed_pin(tmp_path: Path) -> None:
    """Rollback lane: packages.txt moved on but the lock pins the old set."""
    spec = replace(load_packages(), codex_version="latest")
    resolved = resolve_versions(
        spec,
        ENV,
        providers={"codex"},
        fetch=_npm_fetch({"openai/codex": "0.160.0"}, []),
        host_probe=_fail_probe,
    )
    lock_path = write_lock(resolved, tmp_path / "lock.json")

    newer = replace(load_packages(), codex_version="0.200.0")
    replayed = resolve_versions(
        newer, ENV, providers={"codex"}, fetch=_fail_fetch, host_probe=_fail_probe, lock=lock_path
    )
    assert replayed.spec.codex_version == "0.160.0"
    assert replayed.entries["codex"].requested == "0.200.0"
    assert replayed.entries["codex"].version == "0.160.0"


def test_lock_replay_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = replace(load_packages(), codex_version="latest")
    resolved = resolve_versions(
        spec,
        ENV,
        providers={"codex"},
        fetch=_npm_fetch({"openai/codex": "0.160.0"}, []),
        host_probe=_fail_probe,
    )
    lock_path = write_lock(resolved, tmp_path / "lock.json")
    env = {"SBX_VERSIONS_LOCK": str(lock_path)}
    replayed = resolve_versions(
        spec, env, providers={"codex"}, fetch=_fail_fetch, host_probe=_fail_probe
    )
    assert replayed.spec.codex_version == "0.160.0"


def test_devin_lock_replay_restores_checksums_without_fetch(tmp_path: Path) -> None:
    spec = replace(load_packages(), devin_version="latest")
    manifest = _devin_manifest("3000.99.0", "c" * 64, "d" * 64)
    resolved = resolve_versions(
        spec, ENV, providers={"devin"}, fetch=lambda url: manifest, host_probe=_fail_probe
    )
    lock_path = write_lock(resolved, tmp_path / "lock.json")
    replayed = resolve_versions(
        spec, ENV, providers={"devin"}, fetch=_fail_fetch, host_probe=_fail_probe, lock=lock_path
    )
    assert replayed.spec.devin_version == "3000.99.0"
    assert replayed.spec.devin_sha256_x86_64 == "c" * 64
    assert replayed.spec.devin_sha256_aarch64 == "d" * 64


def test_read_lock_ignores_foreign_or_missing_files(tmp_path: Path) -> None:
    assert read_lock(tmp_path / "absent.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert read_lock(bad) is None
    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps({"schema": "other", "providers": {}}))
    assert read_lock(foreign) is None


def test_offline_resolution_reports_unresolved_latest() -> None:
    """``--manifest``-style evidence: no lock and no network means a
    ``latest`` request reports ``unresolved`` — never a silent fetch."""
    spec = replace(load_packages(), codex_version="latest")
    resolved = resolve_versions(
        spec,
        {"SBX_VERSIONS_LOCK_OUT": "/nonexistent/lock.json"},
        providers={"codex"},
        fetch=_fail_fetch,
        host_probe=_fail_probe,
        offline=True,
    )
    assert resolved.entries["codex"].version is None
    assert resolved.entries["codex"].source == "unresolved"


# ------------------------------------------------------------ image integration


def test_rendered_dockerfile_never_contains_floating_latest() -> None:
    """The guardrail behind "never install floating @latest on each Sandbox
    start": every rendered Dockerfile carries concrete versions."""
    text = render_dockerfile_local(load_packages())
    assert "@latest" not in text
    spec = load_packages()
    assert f"{spec.codex_npm}@{spec.codex_version}" in text


def test_rendered_dockerfile_uses_resolved_spec() -> None:
    spec = replace(load_packages(), codex_version="0.160.0")
    text = render_dockerfile_local(spec)
    assert "@openai/codex@0.160.0" in text


def test_manifest_reports_resolution_evidence() -> None:
    spec = load_packages()
    resolved = resolve_versions(
        spec, ENV, providers={"codex", "devin"}, fetch=_fail_fetch, host_probe=_fail_probe
    )
    manifest = image_manifest(resolved=resolved)
    codex = manifest["providers"]["codex"]["resolution"]
    assert codex["requested"] == spec.codex_version
    assert codex["resolved"] == spec.codex_version
    assert codex["source"] == "pin"
    # Providers outside the resolved set still report a sane fallback.
    agy = manifest["providers"]["antigravity"]["resolution"]
    assert agy["resolved"] == spec.agy_version and agy["source"] == "pin"


def test_manifest_without_resolved_set_marks_latest_unresolved() -> None:
    spec = replace(load_packages(), codex_version="latest")
    manifest = image_manifest(spec)
    codex = manifest["providers"]["codex"]["resolution"]
    assert codex["requested"] == "latest"
    assert codex["resolved"] is None
    assert codex["source"] == "unresolved"


def test_resolve_versions_cli_freezes_lock_and_prints_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out_path = tmp_path / "cli-versions.json"
    monkeypatch.setenv("SBX_VERSIONS_LOCK_OUT", str(out_path))
    assert image_main(["--resolve-versions"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == LOCK_SCHEMA
    assert payload["lock_path"] == str(out_path)
    assert out_path.exists()
    spec = load_packages()
    assert payload["providers"]["codex"]["version"] == spec.codex_version
    # Frozen evidence is itself replayable.
    lock = read_lock(out_path)
    assert lock is not None


def test_lock_out_path_env(tmp_path: Path) -> None:
    assert lock_out_path_for({"SBX_VERSIONS_LOCK_OUT": str(tmp_path / "x.json")}) == (
        tmp_path / "x.json"
    )
    assert lock_out_path_for({}).name == "versions.lock.json"

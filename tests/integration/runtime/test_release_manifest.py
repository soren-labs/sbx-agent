"""Release 0.1 (SOR-61): doctor-compatible image manifest + provider mapping.

``image_manifest()`` is a pure function of ``packages.txt`` + the
``IMAGE_BUILDERS`` registry — no Modal, no network, no host-CLI probe. It is
the machine-readable input for the SOR-98 deploy/doctor CLI (missing/wrong
image detection, pinned ``--version`` verification) and for release evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from runtime.image import (
    IMAGE_BUILDERS,
    PROVIDER_CREDENTIAL_FILES,
    image_for,
    image_manifest,
    load_packages,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PROVIDERS = {"codex", "antigravity", "grok", "opencode", "devin"}


def test_manifest_covers_the_contract_provider_set() -> None:
    manifest = image_manifest()
    assert manifest["schema"] == "sbx-runtime/manifest@1"
    assert manifest["app"] == "sbx-runtime"
    assert manifest["source"] == "runtime/packages.txt"
    assert set(manifest["providers"]) == CONTRACT_PROVIDERS
    assert set(manifest["providers"]) == set(IMAGE_BUILDERS)


def test_manifest_provider_images_match_image_for() -> None:
    manifest = image_manifest()
    for provider, meta in manifest["providers"].items():
        assert meta["image"] == image_for(provider)
        assert meta["image"].startswith("sbx-runtime")


def test_manifest_pins_match_packages_txt() -> None:
    spec = load_packages()
    manifest = image_manifest()
    providers = manifest["providers"]
    assert providers["codex"]["version"] == spec.codex_version
    assert providers["codex"]["install"]["package"] == spec.codex_npm_spec
    assert providers["devin"]["version"] == spec.devin_version
    assert providers["devin"]["install"]["sha256"]["x86_64"] == spec.devin_sha256_x86_64
    assert providers["devin"]["install"]["sha256"]["aarch64"] == spec.devin_sha256_aarch64
    assert providers["antigravity"]["version"] == spec.agy_version
    assert providers["grok"]["version"] == spec.grok_version
    assert providers["opencode"]["version"] == spec.opencode_version
    assert providers["opencode"]["install"]["package"] == spec.opencode_npm_spec


def test_manifest_version_checks_are_doctor_runnable() -> None:
    spec = load_packages()
    manifest = image_manifest()
    expected = {
        "codex": ("/usr/local/bin/codex", spec.codex_version_expect),
        "devin": ("/usr/local/bin/devin", spec.devin_version),
        "antigravity": ("/usr/local/bin/agy", spec.agy_version),
        "grok": ("/usr/local/bin/grok", spec.grok_version),
        "opencode": ("/usr/local/bin/opencode", spec.opencode_version),
    }
    for provider, (cli_path, expect) in expected.items():
        check = manifest["providers"][provider]["version_check"]
        assert check["argv"] == [cli_path, "--version"]
        assert check["expect"] == expect
        assert manifest["providers"][provider]["cli_path"] == cli_path


def test_manifest_install_kinds_and_home_layout() -> None:
    manifest = image_manifest()
    providers = manifest["providers"]
    assert providers["codex"]["install"]["kind"] == "npm"
    assert providers["devin"]["install"]["kind"] == "bundle"
    assert providers["opencode"]["install"]["kind"] == "npm"
    for provider in ("antigravity", "grok"):
        install = providers[provider]["install"]
        assert install["kind"] == "host-binary"
        assert install["env"].startswith("SBX_")
        assert install["default"].startswith("~/")
    for provider in ("antigravity", "grok", "opencode", "devin"):
        assert providers[provider]["env"]["HOME"] == "/work/home"
    layout = manifest["layout"]
    assert layout["work"] == "/work"
    assert layout["home"] == "/work/home"
    assert layout["codex_home"] == "/work/.codex"
    assert layout["dirs"] == ["inbox", "turns", "home", ".codex"]


def test_manifest_credential_files_match_contract() -> None:
    manifest = image_manifest()
    for provider, files in PROVIDER_CREDENTIAL_FILES.items():
        assert manifest["providers"][provider]["credential_files"] == list(files)
    # Spot-check the contract relpaths (docs/contracts/filesystem.md).
    assert manifest["providers"]["codex"]["credential_files"] == [".codex/auth.json"]
    assert manifest["providers"]["devin"]["credential_files"] == [
        ".local/share/devin/credentials.toml"
    ]
    assert manifest["providers"]["opencode"]["credential_files"] == [
        ".local/share/opencode/auth.json"
    ]


def test_manifest_is_json_serializable_and_secret_free() -> None:
    text = json.dumps(image_manifest(), sort_keys=True)
    assert json.loads(text)
    for needle in ("SBX_ACCOUNT_CREDENTIAL", "token =", "api_key", "secret"):
        assert needle not in text


def test_manifest_cli_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--manifest"]) == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["schema"] == "sbx-runtime/manifest@1"
    assert set(parsed["providers"]) == CONTRACT_PROVIDERS


def test_base_pins_recorded_for_release_evidence() -> None:
    spec = load_packages()
    manifest = image_manifest()
    assert manifest["base"]["python_version"] == spec.python_version
    assert manifest["base"]["node_major"] == spec.node_major
    assert manifest["base"]["apt"] == list(spec.apt)

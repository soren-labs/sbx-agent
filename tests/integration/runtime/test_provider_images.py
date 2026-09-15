"""SOR-62/SOR-80 provider fast path: no-cloud checks for agy / grok images.

``sbx_antigravity_image`` / ``sbx_grok_image`` layer the build host's CLI
binary (never committed) onto ``sbx_runtime_image()`` — the same derivation
the SOR-62 gates verified. These tests cover the build *configuration* only:
they never import ``modal`` and never talk to Modal or docker. Real
build/publish + ``agy --version`` / ``grok --version`` in a clean sandbox is a
host-executed gate (``make image-antigravity`` / ``make image-grok``).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from runtime.image import (
    AGY_BIN_ENV,
    AGY_BIN_REMOTE,
    AGY_IMAGE_NAME,
    DEFAULT_AGY_BIN,
    DEFAULT_GROK_BIN,
    GROK_BIN_ENV,
    GROK_BIN_REMOTE,
    GROK_IMAGE_NAME,
    IMAGE_BUILDERS,
    IMAGE_NAME,
    OPENCODE_IMAGE_NAME,
    _assert_host_cli_version,
    _cli_image,
    _host_cli_bin,
    agent_home_env,
    cli_version_check,
    image_for,
    devin_runtime_env,
    load_packages,
    opencode_install_command,
    sbx_opencode_image,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
IMAGE_PY = (REPO_ROOT / "runtime" / "image.py").read_text(encoding="utf-8")
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


class _RecordingImage:
    """Stands in for a modal.Image; records the provider-CLI layer calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def add_local_file(self, local_path, remote_path, copy=False):
        self.calls.append(("add_local_file", local_path, remote_path, copy))
        return self

    def run_commands(self, *commands):
        self.calls.append(("run_commands", commands))
        return self

    def env(self, vars):
        self.calls.append(("env", vars))
        return self


def test_image_names_are_provider_scoped() -> None:
    assert AGY_IMAGE_NAME == "sbx-runtime-antigravity"
    assert GROK_IMAGE_NAME == "sbx-runtime-grok"
    assert OPENCODE_IMAGE_NAME == "sbx-runtime-opencode"
    assert IMAGE_NAME == "sbx-runtime"


def test_image_builders_cover_all_fast_path_providers() -> None:
    assert sorted(IMAGE_BUILDERS) == ["antigravity", "codex", "devin", "grok", "opencode"]
    assert IMAGE_BUILDERS["antigravity"][1] == AGY_IMAGE_NAME
    assert IMAGE_BUILDERS["grok"][1] == GROK_IMAGE_NAME
    assert IMAGE_BUILDERS["opencode"][1] == OPENCODE_IMAGE_NAME
    for builder, _name in IMAGE_BUILDERS.values():
        assert callable(builder)


def test_image_for_is_the_explicit_provider_mapping() -> None:
    assert image_for("codex") == IMAGE_NAME
    assert image_for("devin") == "sbx-runtime-devin"
    assert image_for("antigravity") == AGY_IMAGE_NAME
    assert image_for("grok") == GROK_IMAGE_NAME
    assert image_for("opencode") == OPENCODE_IMAGE_NAME
    for provider in IMAGE_BUILDERS:
        assert image_for(provider) == IMAGE_BUILDERS[provider][1]
    with pytest.raises(KeyError):
        image_for("claude")


def test_control_config_image_names_in_sync() -> None:
    from control.config import ANTIGRAVITY_IMAGE_NAME
    from control.config import GROK_IMAGE_NAME as CFG_GROK
    from control.config import OPENCODE_IMAGE_NAME as CFG_OPENCODE

    assert ANTIGRAVITY_IMAGE_NAME == AGY_IMAGE_NAME
    assert CFG_GROK == GROK_IMAGE_NAME
    assert CFG_OPENCODE == OPENCODE_IMAGE_NAME


def test_agent_home_env_roots_home_under_work() -> None:
    assert agent_home_env() == {"HOME": "/work/home"}
    assert agent_home_env("/x") == {"HOME": "/x/home"}


def test_host_cli_bin_resolves_env_override_and_symlink(tmp_path: Path, monkeypatch) -> None:
    real = tmp_path / "real-cli"
    real.write_bytes(b"\x7fELF-fake")
    link = tmp_path / "cli-link"
    link.symlink_to(real)
    monkeypatch.setenv(AGY_BIN_ENV, str(link))
    assert _host_cli_bin(AGY_BIN_ENV, DEFAULT_AGY_BIN, "agy") == real.resolve()


def test_host_cli_bin_errors_when_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(AGY_BIN_ENV, raising=False)
    with pytest.raises(SystemExit) as exc:
        _host_cli_bin(AGY_BIN_ENV, tmp_path / "nope", "agy")
    assert "agy binary not found" in str(exc.value)
    assert AGY_BIN_ENV in str(exc.value)


def test_cli_image_layers_binary_chmod_and_env(tmp_path: Path) -> None:
    host = tmp_path / "agy"
    host.write_bytes(b"bin")
    base = _RecordingImage()
    out = _cli_image(base, host, AGY_BIN_REMOTE, agent_home_env())
    assert out is base
    assert base.calls == [
        ("add_local_file", str(host), "/usr/local/bin/agy", True),
        ("run_commands", ("chmod 755 /usr/local/bin/agy",)),
        ("env", {"HOME": "/work/home"}),
    ]


def test_cli_image_appends_version_gate_when_expect_given(tmp_path: Path) -> None:
    host = tmp_path / "agy"
    host.write_bytes(b"bin")
    base = _RecordingImage()
    _cli_image(base, host, AGY_BIN_REMOTE, agent_home_env(), version_expect="1.2.2")
    assert base.calls[-1] == (
        "run_commands",
        (cli_version_check(AGY_BIN_REMOTE, "1.2.2"),),
    )


def test_host_cli_version_gate(tmp_path: Path) -> None:
    ok = tmp_path / "grok"
    ok.write_text("#!/bin/sh\necho 'grok 1.0.24 (68e414c661e3) [stable]'\n", encoding="utf-8")
    ok.chmod(0o755)
    out = _assert_host_cli_version(ok, "grok", "1.0.24")
    assert "1.0.24" in out

    wrong = tmp_path / "agy-old"
    wrong.write_text("#!/bin/sh\necho 1.1.0\n", encoding="utf-8")
    wrong.chmod(0o755)
    with pytest.raises(SystemExit) as exc:
        _assert_host_cli_version(wrong, "agy", "1.2.2")
    assert "1.2.2" in str(exc.value)

    # A pin must not match as a prefix of a different version: 1.2.20 and
    # 11.0.24 both contain the pins as substrings but are not the pins.
    for version_out, cli, pin in (
        ("1.2.20", "agy", "1.2.2"),
        ("11.0.24", "grok", "1.0.24"),
        ("1.2.2.1", "agy", "1.2.2"),
    ):
        prefixed = tmp_path / f"{cli}-{version_out}"
        prefixed.write_text(f"#!/bin/sh\necho '{version_out}'\n", encoding="utf-8")
        prefixed.chmod(0o755)
        with pytest.raises(SystemExit):
            _assert_host_cli_version(prefixed, cli, pin)

    broken = tmp_path / "agy-broken"
    broken.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    broken.chmod(0o755)
    with pytest.raises(SystemExit):
        _assert_host_cli_version(broken, "agy", "1.2.2")


def test_cli_version_check_shell_command() -> None:
    assert cli_version_check("opencode", "1.18.29") == (
        r"opencode --version 2>&1 | grep -E '(^|[^0-9.])1\.18\.29([^0-9.]|$)'"
    )
    quoted = cli_version_check("codex", "codex-cli 0.153.0")
    assert quoted.startswith("codex --version")
    assert "grep -E '(^|[^0-9.])" in quoted
    assert "0\\.153\\.0" in quoted


def test_cli_version_check_gate_rejects_prefixed_versions(tmp_path: Path) -> None:
    """The rendered in-image gate fails on versions the pin is a prefix of."""
    fake_cli = tmp_path / "fake-cli"
    script = '#!/bin/sh\necho "$FAKE_VERSION_OUT"\n'
    fake_cli.write_text(script, encoding="utf-8")
    fake_cli.chmod(0o755)

    def run_gate(version_out: str, pin: str) -> int:
        command = cli_version_check(str(fake_cli), pin)
        proc = subprocess.run(
            ["bash", "-c", command],
            env={**os.environ, "FAKE_VERSION_OUT": version_out},
            capture_output=True,
        )
        return proc.returncode

    assert run_gate("grok 1.0.24 (68e414c) [stable]", "1.0.24") == 0
    assert run_gate("1.2.2", "1.2.2") == 0
    assert run_gate("codex-cli 0.153.0", "codex-cli 0.153.0") == 0
    assert run_gate("1.2.20", "1.2.2") != 0
    assert run_gate("11.0.24", "1.0.24") != 0
    assert run_gate("1.2.3", "1.2.2") != 0
    assert run_gate("codex-cli 0.153.01", "codex-cli 0.153.0") != 0


def test_opencode_image_layers_pinned_npm_cli() -> None:
    spec = load_packages()
    base = _RecordingImage()
    out = sbx_opencode_image(base=base, spec=spec)
    assert out is base
    assert base.calls == [
        (
            "run_commands",
            (
                f"npm i -g {spec.opencode_npm_spec}",
                cli_version_check("opencode", spec.opencode_version),
            ),
        ),
        ("env", devin_runtime_env()),
    ]


def test_opencode_install_command_pins_npm_version() -> None:
    """SOR-96: the opencode image layers ``npm i -g opencode-ai@<pin>`` — a
    public registry artifact, so no host binary is needed."""
    spec = load_packages()
    assert spec.opencode_npm == "opencode-ai"
    assert spec.opencode_version
    cmd = opencode_install_command()
    assert f"npm i -g opencode-ai@{spec.opencode_version}" in cmd
    assert "opencode --version" in cmd


def test_opencode_image_layers_npm_and_xdg_env() -> None:
    """``sbx_opencode_image`` = base + npm pin + HOME/XDG pinning (auth.json
    is an XDG data file); no add_local_file and no secrets."""
    import runtime.image as runtime_image

    base = _RecordingImage()
    original = runtime_image.sbx_runtime_image
    runtime_image.sbx_runtime_image = lambda: base
    try:
        out = runtime_image.sbx_opencode_image()
    finally:
        runtime_image.sbx_runtime_image = original
    assert out is base
    kinds = [call[0] for call in base.calls]
    assert "add_local_file" not in kinds
    assert kinds == ["run_commands", "env"]
    run_cmds = base.calls[0][1]
    assert any("npm i -g opencode-ai@" in cmd for cmd in run_cmds)
    env = base.calls[1][1]
    assert env == devin_runtime_env()
    assert env["XDG_DATA_HOME"].endswith("/.local/share")


def test_image_py_defines_provider_builders() -> None:
    assert "def sbx_antigravity_image" in IMAGE_PY
    assert "def sbx_grok_image" in IMAGE_PY
    assert "def sbx_opencode_image" in IMAGE_PY
    assert "sbx_runtime_image()" in IMAGE_PY  # layers on the codex base
    assert AGY_BIN_REMOTE in IMAGE_PY
    assert GROK_BIN_REMOTE in IMAGE_PY
    assert AGY_BIN_ENV in IMAGE_PY and GROK_BIN_ENV in IMAGE_PY
    assert DEFAULT_AGY_BIN.name == "agy" and DEFAULT_GROK_BIN.name == "grok"
    # The binary comes from the build host, not the repo: no committed blob.
    assert "add_local_file" in IMAGE_PY
    # Build/publish entry must not bake secrets or sandbox params.
    for needle in ("Secret.from_dict", "SBX_ACCOUNT_CREDENTIAL", "api_key", "token ="):
        assert needle not in IMAGE_PY
    assert "modal.Sandbox.create" not in IMAGE_PY


def test_cli_supports_provider_flag_and_devin_alias() -> None:
    assert '"--provider"' in IMAGE_PY
    assert '"--devin"' in IMAGE_PY
    assert "build_named_image(provider=" in IMAGE_PY


def test_makefile_exposes_provider_image_targets() -> None:
    assert "image-antigravity:" in MAKEFILE
    assert "image-grok:" in MAKEFILE
    assert "image-opencode:" in MAKEFILE
    assert "--provider antigravity" in MAKEFILE
    assert "--provider grok" in MAKEFILE
    # Release 0.1 OpenCode seam + doctor manifest.
    assert "image-opencode:" in MAKEFILE
    assert "--provider opencode" in MAKEFILE
    assert "image-manifest:" in MAKEFILE
    assert "--manifest" in MAKEFILE
    # Codex / devin targets unchanged.
    assert "python -m runtime.image\n" in MAKEFILE or "\truntime.image\n" in MAKEFILE
    assert "--devin" in MAKEFILE


def test_make_test_does_not_build_provider_images() -> None:
    lines = MAKEFILE.splitlines()
    in_test = False
    block: list[str] = []
    for line in lines:
        if line.startswith("test:"):
            in_test = True
            continue
        if in_test:
            if line and not line[0].isspace():
                break
            block.append(line)
    joined = "\n".join(block)
    assert "runtime.image" not in joined
    assert "image-antigravity" not in joined
    assert "image-grok" not in joined
    assert "image-opencode" not in joined

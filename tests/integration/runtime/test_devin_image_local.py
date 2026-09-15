"""SOR-74 Devin-only fast path: no-cloud verification of sbx-runtime-devin.

Homology tests always run (no docker, no Modal). The docker build test
asserts the pinned ``devin --version``, HOME/XDG layout, credential restore,
and that no ACP/Desktop bridge env is baked into the image; it skips when the
docker daemon is unavailable. ``make test`` must never talk to Modal.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest
from runtime.image import (
    DEVIN_IMAGE_NAME,
    DOCKERFILE_DEVIN_LOCAL,
    ENTRYPOINT_SH,
    INSTALL_DEVIN_REMOTE,
    PACKAGES_TXT,
    RUNTIME_DIR,
    devin_install_command,
    devin_runtime_env,
    load_packages,
    render_dockerfile_local,
)
from tests.integration.runtime.test_image_local import _docker_available

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_DEVIN_SH = RUNTIME_DIR / "install-devin.sh"

DOCKER_SKIP_REASON = (
    "docker is not available in this environment. "
    "No-cloud verification when docker is present: "
    "`docker build -f Dockerfile.devin.local -t sbx-runtime-devin:local .` then assert "
    "`devin --version` starts with 'devin 3000.10.21', HOME=/work/home, "
    "and `runner init --provider devin` restores credentials.toml with mode 600."
)

FAKE_CRED_TOML = (
    'windsurf_api_key = "REDACTED"\n'
    'api_server_url = "https://server.codeium.com"\n'
    'devin_webapp_host = "app.devin.ai"\n'
    'devin_api_url = "https://api.devin.ai"\n'
)


def test_packages_txt_pins_devin_bundle() -> None:
    spec = load_packages(PACKAGES_TXT)
    assert spec.devin_version == "3000.10.21"
    assert spec.devin_base_url == "https://static.devin.ai/cli"
    for sha in (spec.devin_sha256_x86_64, spec.devin_sha256_aarch64):
        assert re.fullmatch(r"[0-9a-f]{64}", sha)


def test_install_command_renders_pins() -> None:
    cmd = devin_install_command()
    spec = load_packages()
    assert f"SBX_DEVIN_VERSION={spec.devin_version}" in cmd
    assert spec.devin_sha256_x86_64 in cmd
    assert spec.devin_sha256_aarch64 in cmd
    assert spec.devin_base_url in cmd
    assert cmd.endswith(f"bash {INSTALL_DEVIN_REMOTE}")


def test_devin_runtime_env_roots_home_under_work() -> None:
    env = devin_runtime_env()
    assert env["HOME"] == "/work/home"
    assert env["XDG_DATA_HOME"] == "/work/home/.local/share"
    assert env["XDG_CONFIG_HOME"] == "/work/home/.config"
    assert env["XDG_CACHE_HOME"] == "/work/home/.cache"
    assert env["XDG_STATE_HOME"] == "/work/home/.local/state"


def test_dockerfile_devin_local_is_generated() -> None:
    on_disk = DOCKERFILE_DEVIN_LOCAL.read_text(encoding="utf-8")
    assert on_disk == render_dockerfile_local(devin=True)
    assert "# Devin fast path (SOR-74)" in on_disk
    assert "SBX_DEVIN_VERSION=3000.10.21" in on_disk
    assert "HOME=/work/home" in on_disk
    assert "XDG_DATA_HOME=/work/home/.local/share" in on_disk
    assert "install-devin.sh" in on_disk
    spec = load_packages()
    assert spec.devin_sha256_x86_64 in on_disk
    assert f"devin --version 2>&1 | grep -F {spec.devin_version}" in on_disk
    instructions = [
        line for line in on_disk.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    joined = "\n".join(instructions)
    assert "idle_timeout" not in joined
    assert "DEVIN_API_KEY" not in joined
    assert "ACP_BACKEND" not in joined
    assert "SBX_ACCOUNT_CREDENTIAL" not in joined
    assert "windsurf_api_key" not in joined


def test_dockerfile_local_unchanged_by_devin_variant() -> None:
    base = render_dockerfile_local()
    assert "install-devin" not in base
    assert "SBX_DEVIN" not in base
    assert "XDG_DATA_HOME" not in base
    # Release 0.1: HOME=$SBX_WORK/home is pinned in the base env for every
    # provider (filesystem.md), not only in the devin variant.
    assert "HOME=/work/home" in base
    assert "/work/home" in base  # mkdir'd at build, not only by the entrypoint


def test_image_py_exposes_devin_image_builder() -> None:
    source = (REPO_ROOT / "runtime" / "image.py").read_text(encoding="utf-8")
    assert "def sbx_devin_image" in source
    assert 'DEVIN_IMAGE_NAME = "sbx-runtime-devin"' in source
    assert DEVIN_IMAGE_NAME == "sbx-runtime-devin"
    assert "sbx_runtime_image()" in source  # layers on the codex base
    assert "devin_install_command" in source
    assert "devin_runtime_env" in source
    assert "install-devin.sh" in source
    assert "modal.Sandbox.create" not in source


def test_install_devin_sh_verifies_sha_and_links_bin() -> None:
    src = INSTALL_DEVIN_SH.read_text(encoding="utf-8")
    assert "sha256sum -c -" in src
    assert "/usr/local/bin/devin" in src
    assert "/opt/devin/cli/_versions" in src
    assert "SBX_DEVIN_VERSION" in src
    assert "x86_64-unknown-linux" in src
    assert "aarch64-unknown-linux" in src
    # No secrets or host auth bridges may be baked into the installer.
    assert "windsurf_api_key" not in src
    assert "DEVIN_API_KEY" not in src


def test_entrypoint_unsets_acp_and_devin_key_env(tmp_path: Path) -> None:
    work = tmp_path / "work"
    env = os.environ.copy()
    env["SBX_WORK"] = str(work)
    for key in (
        "ACP_BACKEND",
        "DEVIN_API_KEY",
        "DEVIN_V3_API_KEY",
        "DEVIN_LEGACY_API_KEY",
        "DEVIN_ORG_ID",
        "WINDSURF_API_KEY",
        "DEVIN_OUTPOSTS_TOKEN",
    ):
        env[key] = "polluted"
    env["SBX_ACCOUNT_CREDENTIAL"] = "blob"
    proc = subprocess.run(
        ["bash", str(ENTRYPOINT_SH), "printenv"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    keys = {line.split("=", 1)[0] for line in proc.stdout.splitlines() if "=" in line}
    for key in (
        "ACP_BACKEND",
        "DEVIN_API_KEY",
        "DEVIN_V3_API_KEY",
        "DEVIN_LEGACY_API_KEY",
        "DEVIN_ORG_ID",
        "WINDSURF_API_KEY",
        "DEVIN_OUTPOSTS_TOKEN",
    ):
        assert key not in keys
    assert "SBX_WORK" in keys
    # The blob itself is not scrubbed here — the runner needs it for restore.
    assert "SBX_ACCOUNT_CREDENTIAL" in keys
    assert (work / "home").is_dir()


@pytest.fixture(scope="module")
def devin_docker_image() -> str:
    if not _docker_available():
        pytest.skip(DOCKER_SKIP_REASON)
    tag = "sbx-runtime-devin:local"
    subprocess.run(
        ["docker", "build", "-f", "Dockerfile.devin.local", "-t", tag, "."],
        cwd=REPO_ROOT,
        check=True,
    )
    return tag


def test_docker_devin_version_and_clean_env(devin_docker_image: str) -> None:
    version = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "devin", devin_docker_image, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert version.stdout.strip().startswith("devin 3000.10.21")
    env = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "printenv", devin_docker_image],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    keys = {line.split("=", 1)[0] for line in env.stdout.splitlines() if "=" in line}
    values = dict(line.split("=", 1) for line in env.stdout.splitlines() if "=" in line)
    assert values["HOME"] == "/work/home"
    assert values["XDG_DATA_HOME"] == "/work/home/.local/share"
    assert values["SBX_WORK"] == "/work"
    for key in (
        "ACP_BACKEND",
        "DEVIN_API_KEY",
        "DEVIN_V3_API_KEY",
        "DEVIN_LEGACY_API_KEY",
        "DEVIN_ORG_ID",
        "SBX_ACCOUNT_CREDENTIAL",
    ):
        assert key not in keys


def test_docker_devin_init_restores_credentials(devin_docker_image: str) -> None:
    blob = json.dumps(
        {
            "provider": "devin",
            "files": {".local/share/devin/credentials.toml": FAKE_CRED_TOML},
        }
    )
    script = (
        "set -e; "
        "python -m runtime.runner init --provider devin --model swe-2-high; "
        "stat -c %a /work/home/.local/share/devin/credentials.toml; "
        "devin auth status || true"
    )
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            f"SBX_ACCOUNT_CREDENTIAL={blob}",
            "--entrypoint",
            "bash",
            devin_docker_image,
            "-c",
            script,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "600" in proc.stdout.splitlines()
    # The fake key cannot authenticate, but the CLI must find and use the file.
    assert "credentials.toml" in proc.stdout
    assert "Not logged in" not in proc.stdout


def test_docker_devin_entrypoint_sigterm(devin_docker_image: str) -> None:
    name = f"sbx-devin-ep-{os.getpid()}-{int(time.time())}"
    try:
        subprocess.run(
            ["docker", "run", "-d", "--name", name, devin_docker_image],
            check=True,
            capture_output=True,
            text=True,
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["docker", "exec", name, "test", "-d", "/work/home"],
                check=False,
                capture_output=True,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.1)
        else:
            pytest.fail("devin container entrypoint did not create /work/home")
        t0 = time.monotonic()
        subprocess.run(["docker", "kill", "--signal=TERM", name], check=True, capture_output=True)
        while time.monotonic() - t0 < 5.0:
            inspect = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", name],
                check=True,
                capture_output=True,
                text=True,
            )
            if inspect.stdout.strip() == "false":
                break
            time.sleep(0.05)
        else:
            pytest.fail("entrypoint did not exit within 5s of SIGTERM")
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)

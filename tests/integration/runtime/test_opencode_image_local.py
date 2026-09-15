"""Release 0.1 OpenCode seam: no-cloud verification of sbx-runtime-opencode.

``sbx_opencode_image`` layers the pinned ``opencode-ai`` npm package onto
``sbx_runtime_image()`` — fully reproducible from ``packages.txt`` (no host
artifact, unlike agy/grok). Homology tests always run; the docker build test
asserts the pinned ``opencode --version`` and contract HOME layout, skipping
when the docker daemon is unavailable. ``make test`` never talks to Modal.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from runtime.image import (
    DOCKERFILE_OPENCODE_LOCAL,
    OPENCODE_IMAGE_NAME,
    cli_version_check,
    image_for,
    load_packages,
    render_dockerfile_local,
)
from tests.integration.runtime.test_image_local import _docker_available

REPO_ROOT = Path(__file__).resolve().parents[3]
IMAGE_PY = (REPO_ROOT / "runtime" / "image.py").read_text(encoding="utf-8")

DOCKER_SKIP_REASON = (
    "docker is not available in this environment. "
    "No-cloud verification when docker is present: "
    "`docker build -f Dockerfile.opencode.local -t sbx-runtime-opencode:local .` then assert "
    "`opencode --version` reports the packages.txt pin and HOME=/work/home."
)


def test_packages_txt_pins_opencode() -> None:
    spec = load_packages()
    assert spec.opencode_npm == "opencode-ai"
    assert spec.opencode_version
    assert spec.opencode_npm_spec == f"opencode-ai@{spec.opencode_version}"


def test_dockerfile_opencode_local_is_generated() -> None:
    on_disk = DOCKERFILE_OPENCODE_LOCAL.read_text(encoding="utf-8")
    assert on_disk == render_dockerfile_local(opencode=True)
    assert "# OpenCode fast path (Release 0.1)" in on_disk
    spec = load_packages()
    assert f"npm i -g {spec.opencode_npm_spec}" in on_disk
    assert cli_version_check("opencode", spec.opencode_version) in on_disk
    assert "HOME=/work/home" in on_disk
    instructions = [
        line for line in on_disk.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    joined = "\n".join(instructions)
    assert "idle_timeout" not in joined
    assert "SBX_ACCOUNT_CREDENTIAL" not in joined
    assert "OPENCODE_API_KEY" not in joined


def test_render_rejects_combined_provider_variants() -> None:
    with pytest.raises(ValueError):
        render_dockerfile_local(devin=True, opencode=True)


def test_image_py_exposes_opencode_builder() -> None:
    assert "def sbx_opencode_image" in IMAGE_PY
    assert 'OPENCODE_IMAGE_NAME = "sbx-runtime-opencode"' in IMAGE_PY
    assert OPENCODE_IMAGE_NAME == "sbx-runtime-opencode"
    assert image_for("opencode") == OPENCODE_IMAGE_NAME
    assert "modal.Sandbox.create" not in IMAGE_PY


@pytest.fixture(scope="module")
def opencode_docker_image() -> str:
    if not _docker_available():
        pytest.skip(DOCKER_SKIP_REASON)
    tag = "sbx-runtime-opencode:local"
    subprocess.run(
        ["docker", "build", "-f", "Dockerfile.opencode.local", "-t", tag, "."],
        cwd=REPO_ROOT,
        check=True,
    )
    return tag


def test_docker_opencode_version_and_clean_env(opencode_docker_image: str) -> None:
    spec = load_packages()
    version = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "opencode", opencode_docker_image, "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert spec.opencode_version in version.stdout
    env = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "printenv", opencode_docker_image],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    values = dict(line.split("=", 1) for line in env.stdout.splitlines() if "=" in line)
    keys = set(values)
    assert values["HOME"] == "/work/home"
    assert values["SBX_WORK"] == "/work"
    for key in ("SBX_ACCOUNT_CREDENTIAL", "OPENCODE_API_KEY", "OPENAI_API_KEY"):
        assert key not in keys


def test_docker_opencode_entrypoint_sigterm(opencode_docker_image: str) -> None:
    name = f"sbx-oco-ep-{os.getpid()}-{int(time.time())}"
    try:
        subprocess.run(
            ["docker", "run", "-d", "--name", name, opencode_docker_image],
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
            pytest.fail("opencode container entrypoint did not create /work/home")
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

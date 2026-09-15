"""WP1-A (SOR-29) no-cloud verification of sbx-runtime.

Homology tests always run (no docker, no Modal). The docker build test
asserts pinned ``codex --version`` / Node 22 and SIGTERM on the entrypoint;
it skips with an explanation when the docker daemon is not available.
``make test`` must never call ``python -m runtime.image`` (that talks to Modal).
"""

from __future__ import annotations

import inspect
import os
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest
from runtime.image import (
    DOCKERFILE_LOCAL,
    ENTRYPOINT_SH,
    FORBIDDEN_APT_PREFIXES,
    IMAGE_NAME,
    PACKAGES_TXT,
    PYTHONPATH_REMOTE,
    REQUIRED_APT,
    RUNTIME_DIR,
    RUNTIME_REMOTE,
    invoke_control_deploy,
    load_packages,
    render_dockerfile_local,
    write_local_secrets,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKER_SKIP_REASON = (
    "docker is not available in this environment. "
    "No-cloud verification when docker is present: "
    "`docker build -f Dockerfile.local -t sbx-runtime:local .` then assert "
    "`codex --version` == 'codex-cli 0.153.0', `node --version` starts with v22, "
    "and `kill -TERM` on the entrypoint exits within 5s. "
    "True `modal run` / Sandbox.create is executed by the orchestrator on WSL (SOR-29)."
)


def _docker_available() -> bool:
    if os.environ.get("SBX_SKIP_DOCKER") == "1":
        return False
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        proc = subprocess.run(
            [exe, "info"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def test_packages_txt_pins_p0_recipe() -> None:
    spec = load_packages(PACKAGES_TXT)
    assert spec.python_version == "3.12"
    assert spec.node_major == "22"
    assert spec.codex_npm == "@openai/codex"
    assert spec.codex_version == "0.153.0"
    assert spec.codex_npm_spec == "@openai/codex@0.153.0"
    assert spec.nodesource_setup_url == "https://deb.nodesource.com/setup_22.x"
    for pkg in REQUIRED_APT:
        assert pkg in spec.apt
    for pkg in spec.apt:
        lower = pkg.lower()
        assert not any(lower == p or lower.startswith(f"{p}-") for p in FORBIDDEN_APT_PREFIXES)


def test_packages_txt_pins_provider_cli_versions() -> None:
    """Release 0.1: every provider CLI version is pinned in packages.txt."""
    spec = load_packages(PACKAGES_TXT)
    assert spec.opencode_npm == "opencode-ai"
    assert spec.opencode_npm_spec == f"opencode-ai@{spec.opencode_version}"
    assert spec.codex_version_expect == f"codex-cli {spec.codex_version}"
    # SOR-60 spike-validated host-binary pins.
    assert spec.agy_version == "1.2.2"
    assert spec.grok_version == "1.0.24"
    assert spec.devin_version == "3000.10.21"


def test_dockerfile_local_is_generated_from_packages_txt() -> None:
    rendered = render_dockerfile_local()
    on_disk = DOCKERFILE_LOCAL.read_text(encoding="utf-8")
    assert on_disk == rendered
    spec = load_packages()
    assert f"FROM python:{spec.python_version}-slim-bookworm" in on_disk
    assert f"COPY runtime {RUNTIME_REMOTE}" in on_disk
    assert f"PYTHONPATH={PYTHONPATH_REMOTE}" in on_disk
    assert "COPY runtime/entrypoint.sh" in on_disk
    assert spec.codex_npm_spec in on_disk
    assert spec.nodesource_setup_url in on_disk
    for pkg in spec.apt:
        assert pkg in on_disk
    assert "HOME=/work/home" in on_disk
    assert f"codex --version 2>&1 | grep -F 'codex-cli {spec.codex_version}'" in on_disk
    instructions = [
        line for line in on_disk.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    joined = "\n".join(instructions)
    assert "idle_timeout" not in joined
    assert "CODEX_AUTH_JSON" not in joined
    assert "cpu=" not in joined
    assert "memory=" not in joined


def test_image_py_reads_packages_and_does_not_bake_sandbox_params() -> None:
    source = (REPO_ROOT / "runtime" / "image.py").read_text(encoding="utf-8")
    assert "load_packages" in source
    assert "debian_slim(python_version=spec.python_version)" in source
    assert ".apt_install(*spec.apt)" in source
    assert "codex_npm_spec" in source
    assert "nodesource_setup_url" in source
    assert ".entrypoint(" in source
    assert "add_local_dir" in source
    assert RUNTIME_REMOTE in source
    assert "PYTHONPATH" in source
    # Hardware / lifetime / secrets belong to Sandbox.create (control plane).
    assert "Sandbox.create" in source
    assert "idle_timeout" in source  # mentioned as NOT set here
    assert "modal.Sandbox.create" not in source
    assert "Secret.from_dict" not in source
    assert "cpu=(" not in source
    assert "memory=(" not in source


def test_runtime_dir_contains_runner_package() -> None:
    assert (RUNTIME_DIR / "__init__.py").is_file()
    assert (RUNTIME_DIR / "runner" / "__init__.py").is_file()
    assert (RUNTIME_DIR / "runner" / "__main__.py").is_file()


def test_makefile_test_does_not_build_modal_image() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "python -m runtime.image" in makefile
    lines = makefile.splitlines()
    test_block: list[str] = []
    in_test = False
    for line in lines:
        if line.startswith("test:"):
            in_test = True
            continue
        if in_test:
            if line.startswith("test-") or (line and not line[0].isspace() and line.endswith(":")):
                break
            test_block.append(line)
    joined = "\n".join(test_block)
    assert "pytest" in joined
    assert "runtime.image" not in joined
    assert "modal " not in joined
    assert "\nimage:" in "\n" + makefile
    assert "\ndeploy:" in "\n" + makefile
    assert "\nsecrets:" in "\n" + makefile
    assert "\ntest-e2e-modal:" in "\n" + makefile
    assert "WP2-H" in makefile
    secrets_block: list[str] = []
    in_secrets = False
    for line in lines:
        if line.startswith("secrets:"):
            in_secrets = True
            continue
        if in_secrets:
            if line and not line[0].isspace() and line.endswith(":"):
                break
            secrets_block.append(line)
    secrets_joined = "\n".join(secrets_block)
    assert "write_local_secrets" in secrets_joined
    assert "echo" not in secrets_joined
    assert "cat " not in secrets_joined


def test_entrypoint_exports_contract_home(tmp_path: Path) -> None:
    work = tmp_path / "work"
    env = os.environ.copy()
    env["SBX_WORK"] = str(work)
    proc = subprocess.run(
        ["bash", str(ENTRYPOINT_SH), "printenv", "HOME"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert proc.stdout.strip() == f"{work}/home"


def test_entrypoint_creates_layout_and_exits_on_sigterm(tmp_path: Path) -> None:
    work = tmp_path / "work"
    env = os.environ.copy()
    env["SBX_WORK"] = str(work)
    env.pop("CODEX_HOME", None)
    proc = subprocess.Popen(
        ["bash", str(ENTRYPOINT_SH)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 2.0
    inbox = work / "inbox"
    while time.monotonic() < deadline:
        if inbox.is_dir() and (work / "turns").is_dir() and (work / ".codex").is_dir():
            break
        time.sleep(0.05)
    else:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
        pytest.fail("entrypoint did not create inbox/turns/.codex")
    assert proc.poll() is None
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0
    assert proc.returncode is not None


def test_invoke_control_deploy_imports_control_deploy_not_app_main() -> None:
    source = inspect.getsource(invoke_control_deploy)
    assert "from control.deploy import deploy" in source
    assert "from control.app" not in source
    assert "importlib.import_module" not in source


@pytest.mark.skip(
    reason=(
        "Blocked by SOR-53: control.deploy.deploy() is not on main yet. "
        "WP1-A must not implement control/deploy.py. When WP1-C lands, calling "
        "this would talk to Modal and must stay out of make test."
    )
)
def test_invoke_control_deploy_placeholder(capsys: pytest.CaptureFixture[str]) -> None:
    invoke_control_deploy()
    out = capsys.readouterr().out
    assert "control deploy entry not available yet" in out
    assert "idle_timeout" in out
    assert IMAGE_NAME in out or "sbx-runtime" in out
    assert "token" not in out.lower()
    assert "password" not in out.lower()


def test_write_local_secrets_does_not_print_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token_id = "ak-REDACTED"
    token_secret = "as-REDACTED"
    auth = '{"token":"REDACTED"}'
    monkeypatch.setenv("MODAL_TOKEN_ID", token_id)
    monkeypatch.setenv("MODAL_TOKEN_SECRET", token_secret)
    monkeypatch.setenv("CODEX_AUTH_JSON", auth)
    modal_toml = tmp_path / ".modal.toml"
    auth_json = tmp_path / ".codex" / "auth.json"
    write_local_secrets(modal_toml=modal_toml, auth_json=auth_json)
    out = capsys.readouterr().out
    assert token_id not in out
    assert token_secret not in out
    assert auth not in out
    assert "ak-" not in out
    assert "as-" not in out
    text = modal_toml.read_text(encoding="utf-8")
    assert "[sorenlab2026]" in text
    assert "active = true" in text
    assert token_id in text
    assert token_secret in text
    assert stat.S_IMODE(modal_toml.stat().st_mode) == 0o600
    assert auth_json.read_text(encoding="utf-8") == auth
    assert stat.S_IMODE(auth_json.stat().st_mode) == 0o600


@pytest.fixture(scope="module")
def local_docker_image() -> str:
    if not _docker_available():
        pytest.skip(DOCKER_SKIP_REASON)
    tag = "sbx-runtime:local"
    subprocess.run(
        ["docker", "build", "-f", "Dockerfile.local", "-t", tag, "."],
        cwd=REPO_ROOT,
        check=True,
    )
    return tag


def test_docker_runtime_runner_importable(local_docker_image: str) -> None:
    env = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "printenv", local_docker_image, "PYTHONPATH"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert env.stdout.strip() == PYTHONPATH_REMOTE
    probe = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            local_docker_image,
            "-c",
            "import pathlib, runtime.runner; print(pathlib.Path(runtime.runner.__file__))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    path = probe.stdout.strip()
    assert path.startswith(f"{RUNTIME_REMOTE}/runner")
    listing = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "test", local_docker_image, "-d", RUNTIME_REMOTE],
        check=False,
        capture_output=True,
        text=True,
    )
    assert listing.returncode == 0


def test_docker_codex_and_node_versions(local_docker_image: str) -> None:
    spec = load_packages()
    codex = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "codex", local_docker_image, "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert codex.stdout.strip() == f"codex-cli {spec.codex_version}"
    node = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "node", local_docker_image, "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert node.stdout.strip().startswith(f"v{spec.node_major}")


def test_docker_entrypoint_sigterm_within_5s(local_docker_image: str) -> None:
    name = f"sbx-ep-{os.getpid()}-{int(time.time())}"
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-e",
                "SBX_WORK=/work",
                local_docker_image,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["docker", "exec", name, "test", "-d", "/work/inbox"],
                check=False,
                capture_output=True,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.1)
        else:
            pytest.fail("container entrypoint did not create /work/inbox")
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
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)

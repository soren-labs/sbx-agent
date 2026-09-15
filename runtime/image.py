"""Named Modal Image ``sbx-runtime`` (slim, no browser) + provider variants.

Package versions live in ``runtime/packages.txt``. ``Dockerfile.local`` (and
``Dockerfile.devin.local`` for the Devin fast path, SOR-74) are generated from
the same file so local docker verification stays in lockstep with the Modal
images. Sandbox lifetime and hardware (``idle_timeout``, ``timeout``, ``cpu``,
``memory``, ``workdir``, ``tags``, ``secrets``) are **not** set here — the
control plane passes them to ``Sandbox.create``.

Provider fast paths (SOR-74 devin; SOR-62/SOR-80 antigravity + grok): each
named image is ``sbx-runtime`` plus the provider CLI. The Devin CLI is a
pinned, sha256-verified download; ``agy`` / ``grok`` are proprietary host
binaries taken from the building machine (never committed to the repo) — the
same derivation the SOR-62 e2e gates verified.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path(__file__).resolve().parent
REPO_ROOT = RUNTIME_DIR.parent
PACKAGES_TXT = RUNTIME_DIR / "packages.txt"
ENTRYPOINT_SH = RUNTIME_DIR / "entrypoint.sh"
DOCKERFILE_LOCAL = REPO_ROOT / "Dockerfile.local"

APP_NAME = "sbx-runtime"
IMAGE_NAME = "sbx-runtime"
DEVIN_IMAGE_NAME = "sbx-runtime-devin"
# SOR-62/SOR-80 provider fast path: named images = sbx-runtime + the provider
# CLI binary from the build host (see sbx_antigravity_image / sbx_grok_image).
AGY_IMAGE_NAME = "sbx-runtime-antigravity"
GROK_IMAGE_NAME = "sbx-runtime-grok"
AGY_BIN_ENV = "SBX_AGY_BIN"
GROK_BIN_ENV = "SBX_GROK_BIN"
AGY_BIN_REMOTE = "/usr/local/bin/agy"
GROK_BIN_REMOTE = "/usr/local/bin/grok"
DEFAULT_AGY_BIN = Path.home() / ".local" / "bin" / "agy"
DEFAULT_GROK_BIN = Path.home() / ".local" / "bin" / "grok"
ENTRYPOINT_REMOTE = "/opt/sbx/entrypoint.sh"
RUNTIME_REMOTE = "/opt/sbx/runtime"
INSTALL_DEVIN_REMOTE = f"{RUNTIME_REMOTE}/install-devin.sh"
PYTHONPATH_REMOTE = "/opt/sbx"
MODAL_WORKSPACE = "sorenlab2026"
DOCKERFILE_DEVIN_LOCAL = REPO_ROOT / "Dockerfile.devin.local"

REQUIRED_APT = (
    "curl",
    "git",
    "ca-certificates",
    "ripgrep",
    "jq",
    "procps",
    "build-essential",
    "python3-pip",
)
FORBIDDEN_APT_PREFIXES = ("chrome", "chromium")


@dataclass(frozen=True)
class PackageSpec:
    python_version: str
    node_major: str
    codex_npm: str
    codex_version: str
    devin_version: str
    devin_base_url: str
    devin_sha256_x86_64: str
    devin_sha256_aarch64: str
    apt: tuple[str, ...]

    @property
    def nodesource_setup_url(self) -> str:
        return f"https://deb.nodesource.com/setup_{self.node_major}.x"

    @property
    def codex_npm_spec(self) -> str:
        return f"{self.codex_npm}@{self.codex_version}"


def load_packages(path: Path | None = None) -> PackageSpec:
    """Parse ``runtime/packages.txt``."""
    text = (path or PACKAGES_TXT).read_text(encoding="utf-8")
    keys: dict[str, str] = {}
    apt: list[str] = []
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section == "apt":
            pkg = line.split()[0]
            apt.append(pkg)
            continue
        if "=" not in line:
            raise ValueError(f"invalid packages.txt line: {raw!r}")
        key, _, value = line.partition("=")
        keys[key.strip()] = value.strip()

    missing = [
        k
        for k in (
            "python_version",
            "node_major",
            "codex_npm",
            "codex_version",
            "devin_version",
            "devin_base_url",
            "devin_sha256_x86_64",
            "devin_sha256_aarch64",
        )
        if not keys.get(k)
    ]
    if missing:
        raise ValueError(f"packages.txt missing keys: {missing}")
    apt_tuple = tuple(apt)
    have = set(apt_tuple)
    missing_apt = [p for p in REQUIRED_APT if p not in have]
    if missing_apt:
        raise ValueError(f"packages.txt [apt] missing {missing_apt}")
    for pkg in apt_tuple:
        lower = pkg.lower()
        if any(lower == p or lower.startswith(f"{p}-") for p in FORBIDDEN_APT_PREFIXES):
            raise ValueError(f"sbx-runtime must not install a browser package: {pkg}")
    return PackageSpec(
        python_version=keys["python_version"],
        node_major=keys["node_major"],
        codex_npm=keys["codex_npm"],
        codex_version=keys["codex_version"],
        devin_version=keys["devin_version"],
        devin_base_url=keys["devin_base_url"],
        devin_sha256_x86_64=keys["devin_sha256_x86_64"],
        devin_sha256_aarch64=keys["devin_sha256_aarch64"],
        apt=apt_tuple,
    )


def devin_runtime_env(work: str = "/work") -> dict[str, str]:
    """HOME/XDG for the Devin sandbox (filesystem.md: ``HOME=$SBX_WORK/home``).

    Devin CLI resolves ``credentials.toml`` under ``$XDG_DATA_HOME/devin``
    (default ``~/.local/share/devin``). Pinning both HOME and the XDG dirs keeps
    the credential location stable no matter which env a sandbox exec inherits.
    """
    home = f"{work}/home"
    return {
        "HOME": home,
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_STATE_HOME": f"{home}/.local/state",
    }


def devin_install_command(spec: PackageSpec | None = None) -> str:
    """Shell command that installs the pinned Devin CLI bundle inside an image."""
    spec = spec or load_packages()
    return (
        f"SBX_DEVIN_VERSION={spec.devin_version} "
        f"SBX_DEVIN_BASE_URL={spec.devin_base_url} "
        f"SBX_DEVIN_SHA256_X86_64={spec.devin_sha256_x86_64} "
        f"SBX_DEVIN_SHA256_AARCH64={spec.devin_sha256_aarch64} "
        f"bash {INSTALL_DEVIN_REMOTE}"
    )


def render_dockerfile_local(spec: PackageSpec | None = None, *, devin: bool = False) -> str:
    """Dockerfile used for no-cloud docker verification; values from packages.txt.

    ``devin=True`` renders ``Dockerfile.devin.local`` (SOR-74): the same base
    recipe plus the pinned standalone Devin CLI and HOME/XDG pointed at
    ``/work/home``.
    """
    spec = spec or load_packages()
    apt = " ".join(spec.apt)
    env_lines = f"""\
ENV DEBIAN_FRONTEND=noninteractive \\
    SBX_WORK=/work \\
    PYTHONPATH={PYTHONPATH_REMOTE}"""
    if devin:
        env_lines += "".join(
            f" \\\n    {key}={value}" for key, value in devin_runtime_env().items()
        )
    extra_run = ""
    mkdirs = "/work/inbox /work/turns /work/.codex"
    if devin:
        mkdirs += " /work/home"
        extra_run = f"\nRUN {devin_install_command(spec)}\n"
    header = """\
# GENERATED FROM runtime/packages.txt — do not edit by hand.
# Regenerate: python -m runtime.image --write-dockerfile
# Local verification only. Production image: runtime/image.py (Modal debian_slim).
# Sandbox cpu/memory/timeout/idle_timeout/workdir/tags/secrets are NOT baked in;
# the control plane passes them to Sandbox.create."""
    if devin:
        header += "\n# Devin fast path (SOR-74): base recipe + pinned standalone Devin CLI."
    return f"""\
{header}
FROM python:{spec.python_version}-slim-bookworm

{env_lines}

RUN apt-get update \\
 && apt-get install -y --no-install-recommends {apt} \\
 && curl -fsSL {spec.nodesource_setup_url} | bash - \\
 && apt-get install -y --no-install-recommends nodejs \\
 && npm i -g {spec.codex_npm_spec} \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

COPY runtime {RUNTIME_REMOTE}
COPY runtime/entrypoint.sh {ENTRYPOINT_REMOTE}

RUN chmod +x {ENTRYPOINT_REMOTE} \\
 && mkdir -p {mkdirs}
{extra_run}
WORKDIR /work
ENTRYPOINT ["{ENTRYPOINT_REMOTE}"]
"""


def write_dockerfile_local(path: Path | None = None) -> Path:
    dest = path or DOCKERFILE_LOCAL
    dest.write_text(render_dockerfile_local(), encoding="utf-8")
    return dest


def write_dockerfile_devin_local(path: Path | None = None) -> Path:
    dest = path or DOCKERFILE_DEVIN_LOCAL
    dest.write_text(render_dockerfile_local(devin=True), encoding="utf-8")
    return dest


def sbx_runtime_image():
    """Build the Modal Image object (no network). Caller may ``.build(app)``."""
    import modal

    spec = load_packages()
    return (
        modal.Image.debian_slim(python_version=spec.python_version)
        .apt_install(*spec.apt)
        .run_commands(
            f"curl -fsSL {spec.nodesource_setup_url} | bash -",
            "apt-get install -y nodejs",
            f"npm i -g {spec.codex_npm_spec}",
            "codex --version",
        )
        .add_local_file(str(ENTRYPOINT_SH), ENTRYPOINT_REMOTE, copy=True)
        .add_local_dir(RUNTIME_DIR, RUNTIME_REMOTE, copy=True)
        .run_commands(f"chmod +x {ENTRYPOINT_REMOTE}")
        .env({"SBX_WORK": "/work", "PYTHONPATH": PYTHONPATH_REMOTE})
        .entrypoint([ENTRYPOINT_REMOTE])
    )


def sbx_devin_image():
    """Named Modal Image ``sbx-runtime-devin`` (SOR-74 Devin-only fast path).

    ``sbx-runtime`` plus the pinned standalone Devin CLI bundle (sha256-verified
    by ``runtime/install-devin.sh``) and HOME/XDG rooted at ``$SBX_WORK/home``
    so the restored ``credentials.toml`` is the only auth source. No Devin
    Desktop, no ACP bridge, no ``DEVIN_*`` key env is baked in.
    """
    return sbx_runtime_image().run_commands(devin_install_command()).env(devin_runtime_env())


def agent_home_env(work: str = "/work") -> dict[str, str]:
    """``HOME=$SBX_WORK/home`` for provider-CLI images (agy / grok).

    Unlike the Devin bundle these CLIs resolve credentials relative to
    ``$HOME`` only (``.gemini/`` / ``.grok/``), so no XDG pinning is needed.
    """
    return {"HOME": f"{work}/home"}


def _host_cli_bin(env_var: str, default: Path, cli: str) -> Path:
    """Resolve the host CLI binary baked into a provider image.

    The binary is a build-host artifact (never committed): ``env_var``
    overrides the well-known ``~/.local/bin`` default. Symlinks resolve to the
    real file so ``add_local_file`` uploads a regular binary (``grok`` is a
    symlink chain under ``~/.grok``).
    """
    raw = os.environ.get(env_var)
    path = Path(raw).expanduser() if raw else default
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise SystemExit(
            f"{cli} binary not found at {path}; install the {cli} CLI on the "
            f"build host or set {env_var} to its path"
        )
    return resolved


def _cli_image(image: Any, host_bin: Path, remote: str, env: dict[str, str]) -> Any:
    """``image`` + one host CLI binary at ``remote`` (755) + env overlay."""
    return (
        image.add_local_file(str(host_bin), remote, copy=True)
        .run_commands(f"chmod 755 {remote}")
        .env(env)
    )


def sbx_antigravity_image(agy_bin: Path | None = None):
    """Named Modal Image ``sbx-runtime-antigravity`` (SOR-62/SOR-80 fast path).

    ``sbx-runtime`` plus the host ``agy`` binary at ``/usr/local/bin/agy`` —
    the exact derivation the SOR-62 gate verified — with ``HOME`` rooted at
    ``$SBX_WORK/home`` so the restored ``antigravity-oauth-token`` is the only
    auth source. No credential material is baked into the image.
    """
    host = agy_bin or _host_cli_bin(AGY_BIN_ENV, DEFAULT_AGY_BIN, "agy")
    return _cli_image(sbx_runtime_image(), host, AGY_BIN_REMOTE, agent_home_env())


def sbx_grok_image(grok_bin: Path | None = None):
    """Named Modal Image ``sbx-runtime-grok`` (SOR-62/SOR-80 fast path).

    ``sbx-runtime`` plus the host ``grok`` binary at ``/usr/local/bin/grok`` —
    the exact derivation the SOR-62 gate verified — with ``HOME`` rooted at
    ``$SBX_WORK/home`` so the restored ``.grok/auth.json`` is the only auth
    source. No credential material is baked into the image.
    """
    host = grok_bin or _host_cli_bin(GROK_BIN_ENV, DEFAULT_GROK_BIN, "grok")
    return _cli_image(sbx_runtime_image(), host, GROK_BIN_REMOTE, agent_home_env())


def invoke_control_deploy() -> None:
    """``make deploy``: WP1-C ``control.deploy.deploy()`` if present (SOR-47 / SOR-53).

    Must not call ``control.app.main`` (local uvicorn CLI). WP1-A does not
    implement ``control/deploy.py``; until that module lands, print a hint.
    """
    try:
        from control.deploy import deploy as control_deploy
    except ImportError:
        print("control deploy entry not available yet (WP1-C / SOR-53).")
        print("Build the named image with: make image")
        print(
            "Sandbox parameters (idle_timeout, timeout, cpu, memory, workdir, tags, secrets) "
            "are passed by the control plane at Sandbox.create, not baked into sbx-runtime."
        )
        return
    control_deploy()


def _toml_quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_local_secrets(
    *,
    modal_toml: Path | None = None,
    auth_json: Path | None = None,
    workspace: str = MODAL_WORKSPACE,
) -> None:
    """Write Modal + Codex credentials from env into well-known paths.

    Never prints secret values. Used by ``make secrets``. Does not bake
    credentials into the image.
    """
    token_id = os.environ.get("MODAL_TOKEN_ID") or ""
    token_secret = os.environ.get("MODAL_TOKEN_SECRET") or ""
    auth = os.environ.get("CODEX_AUTH_JSON") or ""
    missing = [
        name
        for name, value in (
            ("MODAL_TOKEN_ID", token_id),
            ("MODAL_TOKEN_SECRET", token_secret),
            ("CODEX_AUTH_JSON", auth),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}")

    toml_path = modal_toml or Path.home() / ".modal.toml"
    auth_path = auth_json or Path.home() / ".codex" / "auth.json"
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(
        f"[{workspace}]\n"
        f"token_id = {_toml_quoted(token_id)}\n"
        f"token_secret = {_toml_quoted(token_secret)}\n"
        "active = true\n",
        encoding="utf-8",
    )
    toml_path.chmod(0o600)
    auth_path.write_text(auth, encoding="utf-8")
    auth_path.chmod(0o600)
    print(f"wrote {toml_path} (workspace {workspace})")
    print(f"wrote {auth_path}")


# provider tag -> (image builder, published name). ``codex`` keeps the P1
# base image; the others are the fast-path variants layered on top of it.
IMAGE_BUILDERS: dict[str, tuple[Any, str]] = {
    "codex": (sbx_runtime_image, IMAGE_NAME),
    "devin": (sbx_devin_image, DEVIN_IMAGE_NAME),
    "antigravity": (sbx_antigravity_image, AGY_IMAGE_NAME),
    "grok": (sbx_grok_image, GROK_IMAGE_NAME),
}


def build_named_image(*, provider: str = "codex") -> None:
    """``modal image build`` equivalent: build + publish a named runtime image.

    ``provider`` selects the variant (``sbx-runtime`` for codex; the SOR-74 /
    SOR-80 fast-path images otherwise). Requires Modal credentials and, for
    agy / grok, the provider CLI on the build host. Never called from
    ``make test``.
    """
    import modal

    spec = load_packages()
    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    try:
        builder, name = IMAGE_BUILDERS[provider]
    except KeyError:
        raise SystemExit(f"unknown image provider {provider!r}") from None
    image = builder()
    with modal.enable_output():
        built = image.build(app)
        publish = getattr(built, "publish", None)
        if callable(publish):
            publish(name)
    extra = f", devin {spec.devin_version}" if provider == "devin" else ""
    print(
        f"named image {name} ready "
        f"(python {spec.python_version}, node {spec.node_major}, {spec.codex_npm_spec}{extra})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sbx-runtime image helpers")
    parser.add_argument(
        "--write-dockerfile",
        action="store_true",
        help="Regenerate Dockerfile.local + Dockerfile.devin.local from packages.txt (no Modal)",
    )
    parser.add_argument(
        "--provider",
        choices=sorted(IMAGE_BUILDERS),
        default="codex",
        help="Which named image to build/publish (default: codex -> sbx-runtime)",
    )
    parser.add_argument(
        "--devin",
        action="store_true",
        help="Alias for --provider devin (SOR-74)",
    )
    args = parser.parse_args(argv)
    if args.write_dockerfile:
        for write in (write_dockerfile_local, write_dockerfile_devin_local):
            print(f"wrote {write()}")
        return 0
    build_named_image(provider="devin" if args.devin else args.provider)
    return 0


if __name__ == "__main__":
    sys.exit(main())

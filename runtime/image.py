"""Named Modal Image ``sbx-runtime`` (slim, no browser).

Package versions live in ``runtime/packages.txt``. ``Dockerfile.local`` is
generated from the same file so local docker verification stays in lockstep
with the Modal image. Sandbox lifetime and hardware (``idle_timeout``,
``timeout``, ``cpu``, ``memory``, ``workdir``, ``tags``, ``secrets``) are
**not** set here — WP1-C passes them to ``Sandbox.create``.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parent
REPO_ROOT = RUNTIME_DIR.parent
PACKAGES_TXT = RUNTIME_DIR / "packages.txt"
ENTRYPOINT_SH = RUNTIME_DIR / "entrypoint.sh"
DOCKERFILE_LOCAL = REPO_ROOT / "Dockerfile.local"

APP_NAME = "sbx-runtime"
IMAGE_NAME = "sbx-runtime"
ENTRYPOINT_REMOTE = "/opt/sbx/entrypoint.sh"

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
        k for k in ("python_version", "node_major", "codex_npm", "codex_version") if not keys.get(k)
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
        apt=apt_tuple,
    )


def render_dockerfile_local(spec: PackageSpec | None = None) -> str:
    """Dockerfile used for no-cloud docker verification; values from packages.txt."""
    spec = spec or load_packages()
    apt = " ".join(spec.apt)
    return f"""\
# GENERATED FROM runtime/packages.txt — do not edit by hand.
# Regenerate: python -m runtime.image --write-dockerfile
# Local verification only. Production image: runtime/image.py (Modal debian_slim).
# Sandbox cpu/memory/timeout/idle_timeout/workdir/tags/secrets are NOT baked in;
# the control plane passes them to Sandbox.create.
FROM python:{spec.python_version}-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \\
    SBX_WORK=/work

RUN apt-get update \\
 && apt-get install -y --no-install-recommends {apt} \\
 && curl -fsSL {spec.nodesource_setup_url} | bash - \\
 && apt-get install -y --no-install-recommends nodejs \\
 && npm i -g {spec.codex_npm_spec} \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

COPY runtime/packages.txt /opt/sbx/packages.txt
COPY runtime/entrypoint.sh {ENTRYPOINT_REMOTE}

RUN chmod +x {ENTRYPOINT_REMOTE} \\
 && mkdir -p /work/inbox /work/turns /work/.codex

WORKDIR /work
ENTRYPOINT ["{ENTRYPOINT_REMOTE}"]
"""


def write_dockerfile_local(path: Path | None = None) -> Path:
    dest = path or DOCKERFILE_LOCAL
    dest.write_text(render_dockerfile_local(), encoding="utf-8")
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
        .run_commands(f"chmod +x {ENTRYPOINT_REMOTE}")
        .env({"SBX_WORK": "/work"})
        .entrypoint([ENTRYPOINT_REMOTE])
    )


def invoke_control_deploy() -> None:
    """Placeholder ``make deploy``: call WP1-C if present, otherwise print a hint."""
    candidates = (
        ("control.deploy", "deploy"),
        ("control.deploy", "main"),
        ("control.app", "deploy"),
        ("control.app", "main"),
    )
    for mod_name, attr in candidates:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        fn = getattr(mod, attr, None)
        if callable(fn):
            fn()
            return
    print("control deploy entry not available yet (WP1-C).")
    print("Build the named image with: make image")
    print(
        "Sandbox parameters (idle_timeout, timeout, cpu, memory, workdir, tags, secrets) "
        "are passed by the control plane at Sandbox.create, not baked into sbx-runtime."
    )


def build_named_image() -> None:
    """``modal image build`` equivalent: build + publish named image ``sbx-runtime``.

    Requires Modal credentials. Never called from ``make test``.
    """
    import modal

    spec = load_packages()
    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    image = sbx_runtime_image()
    with modal.enable_output():
        built = image.build(app)
        publish = getattr(built, "publish", None)
        if callable(publish):
            publish(IMAGE_NAME)
    print(
        f"named image {IMAGE_NAME} ready "
        f"(python {spec.python_version}, node {spec.node_major}, {spec.codex_npm_spec})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sbx-runtime image helpers")
    parser.add_argument(
        "--write-dockerfile",
        action="store_true",
        help="Regenerate Dockerfile.local from runtime/packages.txt (no Modal)",
    )
    args = parser.parse_args(argv)
    if args.write_dockerfile:
        path = write_dockerfile_local()
        print(f"wrote {path}")
        return 0
    build_named_image()
    return 0


if __name__ == "__main__":
    sys.exit(main())

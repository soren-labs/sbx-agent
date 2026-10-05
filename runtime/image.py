"""Named Modal Image ``sbx-runtime`` + the ``sbx-runtime-opencode`` variant.

Package versions live in ``runtime/packages.txt``. ``Dockerfile.local`` (and
``Dockerfile.opencode.local`` for the harness variant) are generated from the
same file so local docker verification stays in lockstep with the Modal
images. Sandbox lifetime and hardware (``idle_timeout``, ``timeout``,
``cpu``, ``memory``, ``workdir``, ``tags``, ``secrets``) are **not** set here
— the control plane passes them to ``Sandbox.create``.

``sbx-runtime-opencode`` is ``sbx-runtime`` plus the pinned ``opencode-ai``
npm package (public registry artifact). ``image_manifest()`` emits the
machine-readable build metadata tooling consumes
(``python -m runtime.image --manifest``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path(__file__).resolve().parent
REPO_ROOT = RUNTIME_DIR.parent
PACKAGES_TXT = RUNTIME_DIR / "packages.txt"
ENTRYPOINT_SH = RUNTIME_DIR / "entrypoint.sh"
DOCKERFILE_LOCAL = REPO_ROOT / "Dockerfile.local"
DOCKERFILE_OPENCODE_LOCAL = REPO_ROOT / "Dockerfile.opencode.local"

APP_NAME = "sbx-runtime"
IMAGE_NAME = "sbx-runtime"
OPENCODE_IMAGE_NAME = "sbx-runtime-opencode"
OPENCODE_BIN_REMOTE = "/usr/local/bin/opencode"
CLI_VERSION_TIMEOUT_S = 15.0
ENTRYPOINT_REMOTE = "/opt/sbx/entrypoint.sh"
RUNTIME_REMOTE = "/opt/sbx/runtime"
PROTOCOL_REMOTE = "/opt/sbx/protocol"
PYTHONPATH_REMOTE = "/opt/sbx"

# Credential relpaths under $HOME. Recorded in the manifest so tooling can
# verify the credential-path contract.
PROVIDER_CREDENTIAL_FILES: dict[str, tuple[str, ...]] = {
    "opencode": (".local/share/opencode/auth.json",),
}

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
    opencode_npm: str
    opencode_version: str
    apt: tuple[str, ...]

    @property
    def nodesource_setup_url(self) -> str:
        return f"https://deb.nodesource.com/setup_{self.node_major}.x"

    @property
    def opencode_npm_spec(self) -> str:
        return f"{self.opencode_npm}@{self.opencode_version}"


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
            "opencode_npm",
            "opencode_version",
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
        opencode_npm=keys["opencode_npm"],
        opencode_version=keys["opencode_version"],
        apt=apt_tuple,
    )


def cli_runtime_env(work: str = "/work") -> dict[str, str]:
    """HOME/XDG for the sandbox (``HOME=$SBX_WORK/home``).

    The opencode CLI resolves ``auth.json`` under ``$XDG_DATA_HOME/opencode``
    (default ``~/.local/share/opencode``). Pinning both HOME and the XDG dirs
    keeps the credential location stable no matter which env a sandbox exec
    inherits.
    """
    home = f"{work}/home"
    return {
        "HOME": home,
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_STATE_HOME": f"{home}/.local/state",
    }


def _version_grep_pattern(expect: str) -> str:
    """ERE matching ``expect`` as a whole token, not a version prefix.

    Plain substring matching (``grep -F``) false-passes pins that are a
    prefix of a different version — ``1.2.3`` inside ``1.2.30``. Boundaries
    are ``[^0-9.]`` so a match cannot be part of a longer version number on
    either side.
    """
    return r"(^|[^0-9.])" + re.escape(expect) + r"([^0-9.]|$)"


def cli_version_check(cli: str, expect: str) -> str:
    """Image build-step command: ``<cli> --version`` must report ``expect``.

    Runs at image-build time (no cold-start cost). Fails the build when the
    installed CLI is missing, broken, or a different version than the
    packages.txt pin.
    """
    return f"{cli} --version 2>&1 | grep -E {shlex.quote(_version_grep_pattern(expect))}"


def _resolved_spec(spec: PackageSpec | None, providers: set[str], env: Any = None) -> PackageSpec:
    """Spec with ``latest`` requests resolved on the build host.

    Resolution runs once at image build / codegen time — the concrete
    version is frozen into the rendered Dockerfile / image so no sandbox
    ever installs a floating ``@latest``. See ``runtime.versions``.
    """
    if spec is not None:
        return spec
    from runtime.versions import resolve_versions

    return resolve_versions(env=env, providers=frozenset(providers)).spec


def _assert_concrete(provider: str, spec: PackageSpec) -> None:
    """The provider being built must carry a concrete version — never ``latest``."""
    field = {
        "opencode": spec.opencode_version,
    }.get(provider)
    if not field or field == "latest":
        raise SystemExit(
            f"{provider} CLI version is unresolved ({field!r}); resolve it via "
            "runtime.versions before building the image"
        )


def render_dockerfile_local(spec: PackageSpec | None = None, *, opencode: bool = False) -> str:
    """Dockerfile used for no-cloud docker verification; values from packages.txt.

    ``opencode=True`` renders ``Dockerfile.opencode.local``: the same base
    recipe plus the pinned ``opencode-ai`` npm package.

    Without an explicit ``spec``, ``latest`` requests are resolved on the
    build host at generation time so the rendered Dockerfile always carries
    concrete versions.
    """
    spec = spec or _resolved_spec(None, {"opencode"} if opencode else set())
    apt = " ".join(spec.apt)
    env = {
        "DEBIAN_FRONTEND": "noninteractive",
        "SBX_WORK": "/work",
        "PYTHONPATH": PYTHONPATH_REMOTE,
        "HOME": "/work/home",
    }
    if opencode:
        env.update(cli_runtime_env())
    env_lines = "ENV " + " \\\n    ".join(f"{key}={value}" for key, value in env.items())
    extra_run = ""
    mkdirs = "/work/inbox /work/turns /work/home"
    if opencode:
        extra_run = (
            f"\nRUN npm i -g {spec.opencode_npm_spec}\n"
            f"RUN {cli_version_check('opencode', spec.opencode_version)}\n"
        )
    header = """\
# GENERATED FROM runtime/packages.txt — do not edit by hand.
# Regenerate: python -m runtime.image --write-dockerfile
# Local verification only. Production image: runtime/image.py (Modal debian_slim).
# Sandbox cpu/memory/timeout/idle_timeout/workdir/tags/secrets are NOT baked in;
# the control plane passes them to Sandbox.create."""
    if opencode:
        header += "\n# OpenCode variant: base recipe + pinned opencode-ai npm CLI."
    return f"""\
{header}
FROM python:{spec.python_version}-slim-bookworm

{env_lines}

RUN apt-get update \\
 && apt-get install -y --no-install-recommends {apt} \\
 && curl -fsSL {spec.nodesource_setup_url} | bash - \\
 && apt-get install -y --no-install-recommends nodejs \\
 && apt-get clean \\
 && rm -rf /var/lib/apt/lists/*

COPY runtime {RUNTIME_REMOTE}
COPY runtime/entrypoint.sh {ENTRYPOINT_REMOTE}
COPY protocol {PROTOCOL_REMOTE}

RUN chmod +x {ENTRYPOINT_REMOTE} \\
 && mkdir -p {mkdirs}
{extra_run}
WORKDIR /work
ENTRYPOINT ["{ENTRYPOINT_REMOTE}"]
"""


def write_dockerfile_local(path: Path | None = None, *, spec: PackageSpec | None = None) -> Path:
    dest = path or DOCKERFILE_LOCAL
    dest.write_text(render_dockerfile_local(spec), encoding="utf-8")
    return dest


def write_dockerfile_opencode_local(
    path: Path | None = None, *, spec: PackageSpec | None = None
) -> Path:
    dest = path or DOCKERFILE_OPENCODE_LOCAL
    dest.write_text(render_dockerfile_local(spec, opencode=True), encoding="utf-8")
    return dest


def write_dockerfiles_locked(spec: PackageSpec | None = None) -> list[Path]:
    """Regenerate the local Dockerfiles and freeze the resolved set.

    One resolution serves every variant and is recorded in the versions lock
    so the codegen output is reproducible evidence.
    """
    from runtime.versions import resolve_versions, write_lock

    if spec is None:
        resolved = resolve_versions(providers={"opencode"})
        write_lock(resolved)
        spec = resolved.spec
    return [
        write_dockerfile_local(spec=spec),
        write_dockerfile_opencode_local(spec=spec),
    ]


def sbx_runtime_image(spec: PackageSpec | None = None):
    """Build the base Modal Image object (no network). Caller may ``.build(app)``.

    ``spec`` defaults to resolving the build's ``latest`` requests once on
    the build host — the image always installs concrete pins.
    """
    import modal

    spec = spec or _resolved_spec(None, set())
    return (
        modal.Image.debian_slim(python_version=spec.python_version)
        .apt_install(*spec.apt)
        .run_commands(
            f"curl -fsSL {spec.nodesource_setup_url} | bash -",
            "apt-get install -y nodejs",
        )
        .add_local_file(str(ENTRYPOINT_SH), ENTRYPOINT_REMOTE, copy=True)
        .add_local_dir(RUNTIME_DIR, RUNTIME_REMOTE, copy=True)
        .add_local_dir(RUNTIME_DIR.parent / "protocol", PROTOCOL_REMOTE, copy=True)
        .run_commands(f"chmod +x {ENTRYPOINT_REMOTE}")
        .env(
            {
                "SBX_WORK": "/work",
                "PYTHONPATH": PYTHONPATH_REMOTE,
                "HOME": "/work/home",
            }
        )
        .entrypoint([ENTRYPOINT_REMOTE])
    )


def sbx_opencode_image(base: Any | None = None, spec: PackageSpec | None = None):
    """Named Modal Image ``sbx-runtime-opencode``.

    ``sbx-runtime`` plus the pinned ``opencode-ai`` npm package — fully
    reproducible from ``packages.txt``, no host artifact — with ``HOME`` and
    XDG rooted at ``$SBX_WORK/home`` so the restored
    ``.local/share/opencode/auth.json`` is the only auth source.
    ``base``/``spec`` exist for no-cloud tests.
    """
    spec = spec or _resolved_spec(None, {"opencode"})
    image = base if base is not None else sbx_runtime_image(spec)
    return image.run_commands(
        f"npm i -g {spec.opencode_npm_spec}",
        cli_version_check("opencode", spec.opencode_version),
    ).env(cli_runtime_env())


def opencode_install_command(spec: PackageSpec | None = None) -> str:
    """Shell command that installs the pinned OpenCode CLI inside an image."""
    spec = spec or load_packages()
    return f"npm i -g {spec.opencode_npm_spec} && opencode --version"


# provider tag -> (image builder, published name).
IMAGE_BUILDERS: dict[str, tuple[Any, str]] = {
    "runtime": (sbx_runtime_image, IMAGE_NAME),
    "opencode": (sbx_opencode_image, OPENCODE_IMAGE_NAME),
}


def image_for(provider: str) -> str:
    """Explicit provider → published-image-name mapping."""
    try:
        return IMAGE_BUILDERS[provider][1]
    except KeyError:
        raise KeyError(
            f"unknown image provider {provider!r}; known: {sorted(IMAGE_BUILDERS)}"
        ) from None


def _provider_cli_meta(provider: str, spec: PackageSpec) -> dict[str, Any]:
    """Install source + expected ``--version`` evidence for one provider."""
    if provider == "opencode":
        return {
            "cli": "opencode",
            "cli_path": OPENCODE_BIN_REMOTE,
            "install": {"kind": "npm", "package": spec.opencode_npm_spec},
            "version": spec.opencode_version,
            "expect": spec.opencode_version,
        }
    # The bare runtime image carries no provider CLI.
    return {
        "cli": None,
        "cli_path": None,
        "install": {"kind": "none"},
        "version": None,
        "expect": None,
    }


def image_manifest(spec: PackageSpec | None = None, resolved: Any | None = None) -> dict[str, Any]:
    """Release metadata for every named runtime image (evidence).

    Pure function of ``packages.txt`` + the ``IMAGE_BUILDERS`` registry: no
    Modal, no network. ``python -m runtime.image --manifest`` prints it as
    JSON.

    ``resolved`` is an optional ``runtime.versions.ResolvedVersions`` whose
    entries populate ``providers.*.resolution``: the requested pin/``latest``
    vs the concrete frozen version and its provenance.
    """
    spec = spec or load_packages()
    entries = dict(getattr(resolved, "entries", None) or {})
    home_env = {
        "runtime": {},
        # opencode auth.json is an XDG data file: the image pins HOME+XDG.
        "opencode": cli_runtime_env(),
    }
    providers: dict[str, dict[str, Any]] = {}
    for provider in sorted(IMAGE_BUILDERS):
        meta = _provider_cli_meta(provider, spec)
        entry = entries.get(provider)
        if entry is not None:
            resolution = {
                "requested": entry.requested,
                "resolved": entry.version,
                "source": entry.source,
                "evidence": dict(entry.evidence),
            }
        else:
            requested = meta["version"]
            resolution = {
                "requested": requested,
                "resolved": None if requested == "latest" else requested,
                "source": "unresolved" if requested == "latest" else "pin",
                "evidence": {},
            }
        version_check = None
        if meta["cli_path"]:
            version_check = {
                "argv": [meta["cli_path"], "--version"],
                "expect": meta["expect"],
            }
        providers[provider] = {
            "image": image_for(provider),
            "cli": meta["cli"],
            "cli_path": meta["cli_path"],
            "install": meta["install"],
            "version": meta["version"],
            "resolution": resolution,
            "version_check": version_check,
            "env": home_env[provider],
            "credential_files": list(PROVIDER_CREDENTIAL_FILES.get(provider, ())),
        }
    return {
        "schema": "sbx-runtime/manifest@1",
        "app": APP_NAME,
        "source": "runtime/packages.txt",
        "base": {
            "python_version": spec.python_version,
            "node_major": spec.node_major,
            "apt": list(spec.apt),
        },
        "layout": {
            "work": "/work",
            "home": "/work/home",
            "dirs": ["inbox", "turns", "home"],
            "runtime": RUNTIME_REMOTE,
            "protocol": PROTOCOL_REMOTE,
            "entrypoint": ENTRYPOINT_REMOTE,
        },
        "providers": providers,
    }


def build_named_image(
    *,
    provider: str = "opencode",
    name: str | None = None,
    spec: PackageSpec | None = None,
    env: Any = None,
) -> None:
    """``modal image build`` equivalent: build + publish a named runtime image.

    ``provider`` selects the variant. ``name`` overrides the published name
    so a parallel deploy can build RC images without moving the production
    names; ``SBX_IMAGE_APP`` likewise relocates the build app.
    Requires Modal credentials. Never called from ``make test``.

    ``spec`` is the resolved ``PackageSpec``; standalone builds resolve on
    the build host and freeze the outcome to the versions lock.
    """
    import modal

    if spec is None:
        from runtime.versions import lock_out_path_for, resolve_versions, write_lock

        resolved = resolve_versions(env=env, providers={provider})
        write_lock(resolved, lock_out_path_for(env or os.environ))
        spec = resolved.spec
    _assert_concrete(provider, spec)
    app = modal.App.lookup(os.environ.get("SBX_IMAGE_APP") or APP_NAME, create_if_missing=True)
    try:
        builder, default_name = IMAGE_BUILDERS[provider]
    except KeyError:
        raise SystemExit(f"unknown image provider {provider!r}") from None
    publish_name = name or default_name
    image = builder(spec=spec)
    with modal.enable_output():
        built = image.build(app)
        publish = getattr(built, "publish", None)
        if callable(publish):
            publish(publish_name)
    cli_versions = {"opencode": spec.opencode_version}
    extra = f", {provider} {cli_versions[provider]}" if provider in cli_versions else ""
    print(
        f"named image {publish_name} ready (python {spec.python_version}, "
        f"node {spec.node_major}{extra})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sbx-runtime image helpers")
    parser.add_argument(
        "--write-dockerfile",
        action="store_true",
        help="Regenerate Dockerfile.local + Dockerfile.opencode.local from packages.txt (no Modal)",
    )
    parser.add_argument(
        "--manifest",
        action="store_true",
        help="Print the provider-image manifest as JSON (no Modal)",
    )
    parser.add_argument(
        "--resolve-versions",
        action="store_true",
        help="Resolve the provider CLI version (pin or 'latest' request) on the "
        "build host, freeze it to the versions lock, and print the result "
        "as JSON (no Modal)",
    )
    parser.add_argument(
        "--provider",
        choices=sorted(IMAGE_BUILDERS),
        default="opencode",
        help="Which named image to build/publish (default: opencode -> sbx-runtime-opencode)",
    )
    args = parser.parse_args(argv)
    if args.write_dockerfile:
        for path in write_dockerfiles_locked():
            print(f"wrote {path}")
        return 0
    if args.resolve_versions:
        from runtime.versions import lock_out_path_for, resolve_versions, write_lock

        resolved = resolve_versions()
        lock_path = write_lock(resolved, lock_out_path_for(os.environ))
        payload = resolved.lock_payload()
        payload["lock_path"] = str(lock_path)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.manifest:
        from runtime.versions import resolve_versions

        # Offline resolution: pure evidence — a ``latest`` request reports
        # the last frozen lock version when one exists, never a network probe.
        resolved = resolve_versions(offline=True)
        print(json.dumps(image_manifest(resolved=resolved), indent=2, sort_keys=True))
        return 0
    build_named_image(provider=args.provider)
    return 0


if __name__ == "__main__":
    sys.exit(main())

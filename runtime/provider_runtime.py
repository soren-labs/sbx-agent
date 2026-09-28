"""``ProviderRuntimeSpec`` — the per-provider runtime contract (SOR-212/SOR-215).

One registry entry per contract provider consolidates what used to be spread
across ``runtime.image`` (install kind, in-image CLI path, runtime env,
credential relpaths), ``control.onboarding`` (auth/model probe argv, support
tier) and the deploy pipeline (build-host binary lane). The spec is the
single declarative answer to "what does it take to run provider P":

- **distribution** — ``install_kind``: ``npm`` (public registry artifact),
  ``bundle`` (official sha256-verified download) or ``host-binary`` (the
  build host's CLI binary). npm/bundle are fully reproducible from
  ``packages.txt``; ``host-binary`` is the *local-assisted* lane — it needs
  the operator's local CLI and must never block a Platform deploy.
- **version + checksum** — ``spec_field`` names the ``PackageSpec`` version
  pin; ``checksum_fields`` names the sha256 fields a bundle requires.
- **CLI path** — ``cli_path`` is the in-image binary location; ``bin_env``
  the host-side ``*_BIN`` override the probes honour.
- **runtime env** — ``env_kind`` selects the HOME/XDG pinning the image and
  the control plane agree on.
- **credential restore** — ``credential_files`` are the ``$HOME``-relative
  relpaths the ``SBX_ACCOUNT_CREDENTIAL`` blob restores.
- **auth probe / model discovery** — ``auth_argv`` / ``models_argv`` are the
  argv tails each CLI answers with only the restored credential.

``host_assist_problem`` is the deploy-time gate for the local-assisted lane:
it returns a human-readable reason when the lane cannot provision the
provider (missing or wrong-version host CLI) so the caller can mark the
provider ``degraded`` instead of failing the whole deployment.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INSTALL_NPM = "npm"
INSTALL_BUNDLE = "bundle"
INSTALL_HOST_BINARY = "host-binary"

# Runtime env pinning: codex needs nothing beyond the base image env;
# agy/grok resolve credentials under $HOME only; devin/opencode resolve
# credential files through XDG dirs — all rooted at $SBX_WORK/home.
ENV_BASE = "base"
ENV_HOME = "home"
ENV_XDG = "xdg"

LATEST = "latest"


@dataclass(frozen=True)
class ProviderRuntimeSpec:
    """One provider's runtime contract (immutable, declarative)."""

    provider: str
    support: str  # release support tier — mirrors docs/providers.md
    cli: str  # CLI binary name
    cli_path: str  # in-image CLI path
    image_name: str  # default published Modal image name
    bin_env: str  # host-side *_BIN override honoured by auth/model probes
    spec_field: str  # PackageSpec field carrying the CLI version pin
    install_kind: str  # INSTALL_NPM | INSTALL_BUNDLE | INSTALL_HOST_BINARY
    npm_field: str | None = None  # PackageSpec npm-package field (npm kind)
    checksum_fields: tuple[str, ...] = ()  # PackageSpec sha256 fields (bundle)
    host_bin_env: str | None = None  # build-host binary override env
    host_bin_default: str = ""  # default build-host CLI path (host-binary kind)
    env_kind: str = ENV_BASE  # ENV_BASE | ENV_HOME | ENV_XDG
    credential_files: tuple[str, ...] = ()
    auth_argv: tuple[str, ...] = ()
    models_argv: tuple[str, ...] = ()
    default_models: tuple[str, ...] = ()
    summary: str = ""

    @property
    def local_assisted(self) -> bool:
        """True when the CLI ships from the build host (never blocks deploy)."""
        return self.install_kind == INSTALL_HOST_BINARY

    @property
    def reproducible(self) -> bool:
        """True when the distribution is a verifiable remote artifact."""
        return not self.local_assisted

    def version_of(self, spec: Any) -> str:
        """The provider's version pin in a ``runtime.image.PackageSpec``."""
        return str(getattr(spec, self.spec_field))

    def host_bin_path(self, env: Mapping[str, str] | None = None) -> Path:
        """Where the build-host CLI is expected (override or well-known)."""
        src = os.environ if env is None else env
        name = self.host_bin_env or ""
        # Mirror ``runtime.image._host_cli_bin``: the env mapping wins when
        # explicitly set (deploy/test seam), ambient env applies otherwise.
        raw = src.get(name) or os.environ.get(name)
        return Path(raw).expanduser() if raw else Path(self.host_bin_default).expanduser()

    def host_bin(self, env: Mapping[str, str] | None = None) -> Path | None:
        """Resolved build-host CLI binary, or None when absent.

        Non-raising probe counterpart of ``runtime.image._host_cli_bin``:
        deploy asks "can the local-assisted lane build?" instead of dying.
        """
        if not self.local_assisted:
            return None
        resolved = self.host_bin_path(env).resolve()
        return resolved if resolved.is_file() else None

    def runtime_env(self, work: str = "/work") -> dict[str, str]:
        """HOME/XDG pinning this provider's image and sandbox env share."""
        if self.env_kind == ENV_BASE:
            return {}
        if self.env_kind == ENV_HOME:
            return {"HOME": f"{work}/home"}
        from runtime.image import devin_runtime_env

        return devin_runtime_env(work)

    def host_assist_problem(
        self,
        spec: Any,
        env: Mapping[str, str] | None = None,
        *,
        version_output: Callable[[Path], str] | None = None,
    ) -> str | None:
        """Why the local-assisted lane cannot provision this provider.

        Returns ``None`` when the lane is satisfiable — the host CLI is
        present and (for a concrete pin) reports the pinned version. A
        non-``None`` return is a remediation hint, never an exception: the
        caller marks the provider ``degraded`` and the deploy continues —
        the lane may not block a Platform deploy.
        """
        if not self.local_assisted:
            return None
        host = self.host_bin(env)
        if host is None:
            expected = self.host_bin_path(env)
            return (
                f"{self.cli} CLI not found at {expected}; install the {self.cli} "
                f"CLI on the build host or set {self.host_bin_env} to its path"
            )
        expected_version = self.version_of(spec)
        if not expected_version or expected_version == LATEST:
            # The host binary IS the version source for a ``latest`` request —
            # presence is satisfaction.
            return None
        from runtime.image import _cli_version_output, _version_grep_pattern

        probe = version_output or _cli_version_output
        try:
            out = probe(host)
        except SystemExit as exc:
            return str(exc)
        if not re.search(_version_grep_pattern(expected_version), out):
            return (
                f"{self.cli} --version reports {out!r}; packages.txt pins "
                f"{expected_version}. Install the pinned {self.cli} CLI on the "
                "build host or update the pin."
            )
        return None


PROVIDER_RUNTIME_SPECS: tuple[ProviderRuntimeSpec, ...] = (
    ProviderRuntimeSpec(
        provider="codex",
        support="stable",
        cli="codex",
        cli_path="/usr/local/bin/codex",
        image_name="sbx-runtime",
        bin_env="CODEX_BIN",
        spec_field="codex_version",
        install_kind=INSTALL_NPM,
        npm_field="codex_npm",
        env_kind=ENV_BASE,
        credential_files=(".codex/auth.json",),
        auth_argv=("login", "status"),
        models_argv=("debug", "models"),
        default_models=("gpt-5.6-luna",),
        summary="Codex CLI via public npm artifact",
    ),
    ProviderRuntimeSpec(
        provider="devin",
        support="experimental",
        cli="devin",
        cli_path="/usr/local/bin/devin",
        image_name="sbx-runtime-devin",
        bin_env="DEVIN_BIN",
        spec_field="devin_version",
        install_kind=INSTALL_BUNDLE,
        checksum_fields=("devin_sha256_x86_64", "devin_sha256_aarch64"),
        env_kind=ENV_XDG,
        credential_files=(".local/share/devin/credentials.toml",),
        auth_argv=("auth", "status"),
        models_argv=("models", "list", "--format", "json"),
        default_models=("swe-2-high", "swe-2-medium"),
        summary="Official sha256-verified Devin CLI bundle",
    ),
    ProviderRuntimeSpec(
        provider="antigravity",
        support="experimental",
        cli="agy",
        cli_path="/usr/local/bin/agy",
        image_name="sbx-runtime-antigravity",
        bin_env="AGY_BIN",
        spec_field="agy_version",
        install_kind=INSTALL_HOST_BINARY,
        host_bin_env="SBX_AGY_BIN",
        host_bin_default="~/.local/bin/agy",
        env_kind=ENV_HOME,
        # Portable bundle (SOR-258): OAuth token plus the non-secret
        # onboarding marker the 1.2.x CLI requires next to it.  The marker
        # is optional in blobs — the adapter reconstructs it when absent.
        credential_files=(
            ".gemini/antigravity-cli/antigravity-oauth-token",
            ".gemini/antigravity-cli/cache/onboarding.json",
        ),
        auth_argv=("models",),
        models_argv=("models",),
        default_models=("gemini-3.8-flash-low",),
        summary="Anti-Gravity CLI from the build host (local-assisted)",
    ),
    ProviderRuntimeSpec(
        provider="grok",
        support="experimental",
        cli="grok",
        cli_path="/usr/local/bin/grok",
        image_name="sbx-runtime-grok",
        bin_env="GROK_BIN",
        spec_field="grok_version",
        install_kind=INSTALL_HOST_BINARY,
        host_bin_env="SBX_GROK_BIN",
        host_bin_default="~/.local/bin/grok",
        env_kind=ENV_HOME,
        credential_files=(".grok/auth.json",),
        auth_argv=("models",),
        models_argv=("models",),
        default_models=("grok-4.6",),
        summary="Grok CLI from the build host (local-assisted)",
    ),
    ProviderRuntimeSpec(
        provider="opencode",
        support="experimental",
        cli="opencode",
        cli_path="/usr/local/bin/opencode",
        image_name="sbx-runtime-opencode",
        bin_env="OPENCODE_BIN",
        spec_field="opencode_version",
        install_kind=INSTALL_NPM,
        npm_field="opencode_npm",
        env_kind=ENV_XDG,
        credential_files=(".local/share/opencode/auth.json",),
        auth_argv=("auth", "list"),
        models_argv=("models",),
        default_models=("openai/gpt-5.6-luna", "opencode/claude-sonnet-4-5"),
        summary="OpenCode CLI via public npm artifact",
    ),
)

_PROVIDER_INDEX = {s.provider: s for s in PROVIDER_RUNTIME_SPECS}

PROVIDER_RUNTIME_IDS: tuple[str, ...] = tuple(_PROVIDER_INDEX)


def provider_runtime_specs() -> tuple[ProviderRuntimeSpec, ...]:
    """Every provider the runtime contract supports (catalog order)."""
    return PROVIDER_RUNTIME_SPECS


def runtime_spec(provider: str) -> ProviderRuntimeSpec:
    """The spec for one provider; ``KeyError`` for a non-contract id."""
    return _PROVIDER_INDEX[provider]


def spec_or_none(provider: str) -> ProviderRuntimeSpec | None:
    return _PROVIDER_INDEX.get(provider)

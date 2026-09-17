"""Optional GitHub auth bridge (SOR-117).

Repo-native collaboration — private ``git clone``/``push`` and pull-request
creation — needs a GitHub token inside the sandbox. This module is the single
provider-agnostic seam for it (it replaces the Devin-only ``SBX_GITHUB_EPHEMERAL``
bridge in ``control.backends.modal``):

- ``secret_env``/``exec_env`` build the sandbox env **only** when the operator
  exported ``SBX_GITHUB_EPHEMERAL=1`` *and* a ``GH_TOKEN``/``GITHUB_TOKEN`` is
  present in the control-plane env — explicit opt-in, never ambient.
- The token travels by env only: the ``GIT_CONFIG_*`` credential helper echoes
  ``$GH_TOKEN`` at git runtime, so the value never lands in argv, git config,
  or any file inside the sandbox.
- ``detect`` reports host GitHub auth (env vars / ``gh auth status``) by
  *source name and status* — a detection result never carries token material.
- ``redact_url_credentials`` keeps userinfo out of error text.

Without the opt-in the seam emits nothing and the GitHub-less path is
untouched: public clones and local/file remotes work exactly as before.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

# Explicit opt-in: the control-plane env must carry both the gate flag AND a
# token before anything is injected into a sandbox.
GATE_ENV = "SBX_GITHUB_EPHEMERAL"
TOKEN_ENVS = ("GH_TOKEN", "GITHUB_TOKEN")

# Scoped to github.com HTTPS git operations only — the helper answers
# ``x-access-token`` + ``$GH_TOKEN`` at runtime, so the token is never on
# argv, on disk, or in a cloned repo's config. ``credential.helper=`` (empty)
# resets the inherited helper list so nothing ambient answers first.
_GIT_CREDENTIAL_KEYS = (
    ("credential.helper", ""),
    (
        "credential.https://github.com.helper",
        '!f() { echo "username=x-access-token"; echo "password=$GH_TOKEN"; }; f',
    ),
)

# Child env for the host-side ``gh auth status`` probe — scrubbed like
# ``sbx.credentials.cli_auth_check`` so the result reflects gh's own stored
# auth, not ambient token vars.
_PROBE_ENV_KEYS = (
    "LANG",
    "LC_ALL",
    "TERM",
    "SSL_CERT_FILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "ALL_PROXY",
)


def resolve_token(env: Mapping[str, str] | None = None) -> str | None:
    """First non-empty ``GH_TOKEN``/``GITHUB_TOKEN`` value, or ``None``.

    Returns the *value* — callers use it to build env, never to log or render.
    """
    env = os.environ if env is None else env
    for name in TOKEN_ENVS:
        value = env.get(name)
        if value and value.strip():
            return value
    return None


def token_source(env: Mapping[str, str] | None = None) -> str | None:
    """Which env var carries the token (``GH_TOKEN`` wins) — name only."""
    env = os.environ if env is None else env
    for name in TOKEN_ENVS:
        value = env.get(name)
        if value and value.strip():
            return name
    return None


def opted_in(env: Mapping[str, str] | None = None) -> bool:
    """Whether the operator armed the GitHub bridge (``SBX_GITHUB_EPHEMERAL=1``)."""
    env = os.environ if env is None else env
    return env.get(GATE_ENV) == "1"


def injection_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Gate AND token both present — the only state that injects."""
    return opted_in(env) and resolve_token(env) is not None


def secret_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Token env for a Modal ``Secret.from_dict`` / sandbox exec.

    Both ``GH_TOKEN`` and ``GITHUB_TOKEN`` are populated from the resolved
    token so git, ``gh`` and API clients find whichever name they read. Empty
    unless :func:`injection_enabled` — an opt-in flag alone injects nothing.
    """
    if not injection_enabled(env):
        return {}
    token = resolve_token(env)
    assert token is not None
    return {name: token for name in TOKEN_ENVS}


def git_config_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``GIT_CONFIG_*`` entries wiring the github.com credential helper.

    Values contain the literal ``$GH_TOKEN`` reference — git expands it inside
    the sandbox process env when a github.com credential is requested. Empty
    unless injection is enabled.
    """
    if not injection_enabled(env):
        return {}
    out: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0"}
    for index, (key, value) in enumerate(_GIT_CREDENTIAL_KEYS):
        out[f"GIT_CONFIG_KEY_{index}"] = key
        out[f"GIT_CONFIG_VALUE_{index}"] = value
    out["GIT_CONFIG_COUNT"] = str(len(_GIT_CREDENTIAL_KEYS))
    return out


def exec_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Full GitHub overlay for a sandbox exec env (token + git wiring)."""
    return {**secret_env(env), **git_config_env(env)}


# Env keys the overlay owns — ``sandbox_env`` strips them from caller ``extra``
# so the opt-in seam is the only way they enter a sandbox. The GIT_* entries
# beyond the overlay's own slots cover the *alternate* credential/config
# channels (env-config string, askpass, SSH command) — a caller's ``extra``
# must never smuggle a helper or prompt path that bypasses the seam.
OWNED_ENV_KEYS: tuple[str, ...] = (
    *TOKEN_ENVS,
    "GIT_TERMINAL_PROMPT",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_SSH_COMMAND",
)


def owns_env_key(key: str) -> bool:
    """True for token vars, the overlay's ``GIT_CONFIG_*`` slots, and the
    alternate git credential channels (askpass / env-config / SSH command)."""
    return key in OWNED_ENV_KEYS or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))


def redact_url_credentials(url: Any) -> str:
    """``url`` with any ``user:pass@`` userinfo stripped (``<redacted>@``).

    Repo declarations can arrive as ``https://user:TOKEN@host/path``; error
    messages must never echo the credential portion back into run records or
    API responses. Non-URL input is returned unchanged (``repr``-safe callers
    apply ``!r`` themselves).
    """
    if not isinstance(url, str):
        return url
    scheme_sep = url.find("://")
    if scheme_sep < 0:
        return url
    at = url.find("@", scheme_sep + 3)
    if at < 0:
        return url
    slash = url.find("/", scheme_sep + 3)
    if slash >= 0 and slash < at:
        return url  # the '@' is past the authority — not userinfo
    return f"{url[: scheme_sep + 3]}<redacted>@{url[at + 1 :]}"


_GITHUB_HTTPS_RE = re.compile(r"^https://(?:[^@/]+@)?github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
_GITHUB_SSH_RE = re.compile(
    r"^(?:git@github\.com:|ssh://git@github\.com(?::\d+)?/)([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def repo_slug(url: Any) -> str | None:
    """``owner/repo`` for a github.com clone URL, else ``None``.

    Accepts HTTPS (with or without userinfo) and SSH forms; rejects anything
    else so API calls only ever target github.com.
    """
    if not isinstance(url, str):
        return None
    match = _GITHUB_HTTPS_RE.match(url) or _GITHUB_SSH_RE.match(url)
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}"


@dataclass(frozen=True)
class GitHubDetection:
    """Host GitHub auth posture — names/statuses only, never token material."""

    token_env: str | None  # which of TOKEN_ENVS carries a token, or None
    opted_in: bool  # SBX_GITHUB_EPHEMERAL=1
    gh_on_path: bool  # `gh` binary found
    gh_authenticated: bool | None  # None = not probed (verify-gated)

    @property
    def detected(self) -> bool:
        return self.token_env is not None or self.gh_authenticated is True


def _find_gh(env: Mapping[str, str], which: Callable[[str], str | None] | None) -> str | None:
    """Locate the ``gh`` binary; ``which=None`` resolves against ``env`` PATH."""
    if which is not None:
        return which("gh")
    return shutil.which("gh", path=env.get("PATH"))


def gh_auth_status(
    env: Mapping[str, str] | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] | None = None,
    timeout_s: float = 10.0,
) -> bool | None:
    """``gh auth status`` probe: ``True``/``False``/``None`` (not probed).

    Runs with a scrubbed env (no ``GH_TOKEN``/``GITHUB_TOKEN``) so the answer
    reflects gh's own stored auth; the output — which may name accounts and
    hosts — is captured but never returned or rendered.
    """
    env = os.environ if env is None else env
    binary = _find_gh(env, which)
    if binary is None:
        return None
    child_env = {"PATH": env.get("PATH") or os.defpath}
    home = env.get("HOME")
    if home:
        child_env["HOME"] = home
    for key in _PROBE_ENV_KEYS:
        if env.get(key):
            child_env[key] = env[key]
    try:
        proc = runner(
            [binary, "auth", "status"],
            capture_output=True,
            text=True,
            env=child_env,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return getattr(proc, "returncode", -1) == 0


def detect(
    env: Mapping[str, str] | None = None,
    *,
    probe_gh: bool = False,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] | None = None,
) -> GitHubDetection:
    """Detect host GitHub auth without exposing any secret.

    ``token_env`` names the ambient variable holding a token (never its
    value). The ``gh auth status`` probe only runs under ``probe_gh`` (the
    ``--verify`` flag) — same contract as provider credential auth checks.
    """
    env = os.environ if env is None else env
    gh_path = _find_gh(env, which)
    return GitHubDetection(
        token_env=token_source(env),
        opted_in=opted_in(env),
        gh_on_path=gh_path is not None,
        gh_authenticated=(
            gh_auth_status(env, runner=runner, which=which) if probe_gh and gh_path else None
        ),
    )


__all__ = [
    "GATE_ENV",
    "OWNED_ENV_KEYS",
    "TOKEN_ENVS",
    "GitHubDetection",
    "detect",
    "exec_env",
    "gh_auth_status",
    "git_config_env",
    "injection_enabled",
    "opted_in",
    "owns_env_key",
    "redact_url_credentials",
    "repo_slug",
    "resolve_token",
    "secret_env",
    "token_source",
]

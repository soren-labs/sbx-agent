"""Control-plane constants. Values come from contracts + P0 (SOR-28)."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

DEFAULT_MODEL = "gpt-5.6-luna"
MAX_CONCURRENT = 2
IDLE_TIMEOUT_S = 1800
# SOR-80: a ``creating`` record without ``sandbox_id`` is an in-flight create;
# the reaper leaves it alone for this long before declaring it ``lost``.
CREATE_GRACE_S = 300
SANDBOX_TIMEOUT_S = 14400  # 4h hard cap
SSE_KEEPALIVE_S = 15.0
TURN_MAX_SECONDS = 900
# SOR-80: a ``running`` record is finalized by the in-process watcher. When a
# control-plane cutover kills that watcher mid-turn, nothing ever closes the
# record — the reaper treats a turn stale beyond the runner's own
# --max-seconds bound plus this margin as stranded.
RUN_GRACE_S = 300
CPU = (1, 2)
MEMORY_MIB = (1024, 4096)
WORK_DIR = "/work"
CODEX_HOME = "/work/.codex"
MODAL_APP_NAME = "sbx-control"
SESSIONS_DICT_NAME = "sbx-sessions"
RUNS_DICT_NAME = "sbx-runs"
ACCOUNTS_DICT_NAME = "sbx-accounts"
WORKFLOWS_DICT_NAME = "sbx-workflows"
CODEX_SECRET_NAME = "sbx-codex-auth"
BASIC_SECRET_NAME = "sbx-basic-auth"
V1_BOOTSTRAP_SECRET_NAME = "sbx-v1-bootstrap"
# Durable stores without a P0-era home: artifacts (SOR-83) and workspaces
# (SOR-83) live here so ``sbx.config`` can default to the same contract names
# without importing the store modules.
ARTIFACTS_DICT_NAME = "sbx-artifacts"
WORKSPACES_DICT_NAME = "sbx-workspaces"
# Naming convention for per-account credential Secrets: ``sbx-acct-<id>``
# (control/api_v1/bootstrap.py, control/onboarding.py). Operators point it at
# a deployment-scoped prefix (``SBX_ACCOUNT_SECRET_PREFIX``) so a parallel
# deploy's teardown never sweeps another deployment's account Secrets.
ACCOUNT_SECRET_PREFIX = "sbx-acct-"
RUNTIME_IMAGE_NAME = "sbx-runtime"
# SOR-74: provider=devin sandboxes use this named image (sbx-runtime + pinned
# standalone Devin CLI, HOME=$SBX_WORK/home). Keep in sync with
# runtime.image.DEVIN_IMAGE_NAME.
DEVIN_IMAGE_NAME = "sbx-runtime-devin"
# SOR-62/SOR-80: provider=antigravity / grok sandboxes use these named images
# (sbx-runtime + the provider CLI at /usr/local/bin). Keep in sync with
# runtime.image.AGY_IMAGE_NAME / GROK_IMAGE_NAME.
ANTIGRAVITY_IMAGE_NAME = "sbx-runtime-antigravity"
GROK_IMAGE_NAME = "sbx-runtime-grok"
OPENCODE_IMAGE_NAME = "sbx-runtime-opencode"

# Modal Starter sandbox list price (P0): billed at the request floor.
CPU_USD_PER_CORE_S = 0.00003942
MEM_USD_PER_GIB_S = 0.00000667
REQUEST_CPU_CORES = 1.0
REQUEST_MEM_GIB = MEMORY_MIB[0] / 1024.0
SANDBOX_USD_PER_S = CPU_USD_PER_CORE_S * REQUEST_CPU_CORES + MEM_USD_PER_GIB_S * REQUEST_MEM_GIB

TERMINAL_STATUSES = frozenset({"closed", "timed_out", "lost"})
ACTIVE_STATUSES = frozenset({"creating", "idle", "running"})


def env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def account_secret_prefix() -> str:
    """Prefix for per-account credential Secret names (``<prefix><id>``)."""
    return env_str("SBX_ACCOUNT_SECRET_PREFIX", ACCOUNT_SECRET_PREFIX)


def selected_providers(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Deploy-selected providers (``SBX_PROVIDERS``, comma-separated)."""
    env = os.environ if env is None else env
    raw = env.get("SBX_PROVIDERS", "codex")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def app_secret_names(env: Mapping[str, str] | None = None) -> list[str]:
    """Secrets the control app mounts at deploy time (``control/modal_app.py``).

    The shared Codex credential Secret is only required when ``codex`` is a
    selected provider (``SBX_PROVIDERS``) — an unselected provider's
    credential must never block a deploy (SOR-115).
    """
    env = os.environ if env is None else env
    names = [
        env.get("SBX_BASIC_SECRET_NAME") or BASIC_SECRET_NAME,
        env.get("SBX_V1_BOOTSTRAP_SECRET_NAME") or V1_BOOTSTRAP_SECRET_NAME,
    ]
    if "codex" in selected_providers(env):
        names.insert(0, env.get("SBX_CODEX_SECRET_NAME") or CODEX_SECRET_NAME)
    return names


def basic_credentials() -> tuple[str, str]:
    user = os.environ.get("SBX_BASIC_USER") or os.environ.get("SBX_API_USER", "sbx")
    # SBX_BASIC_PASSWORD: pre-0.1 docs used this name; keep accepting it so a
    # Secret written that way still reaches the app instead of silently
    # falling back to the default password.
    password = (
        os.environ.get("SBX_BASIC_PASS")
        or os.environ.get("SBX_BASIC_PASSWORD")
        or os.environ.get("SBX_API_PASSWORD", "sbx")
    )
    return user, password


# Env vars a deploy may forward into the remote control functions. This is
# an allowlist, not a prefix rule: credential material (``SBX_API_KEY``,
# ``SBX_V1_BOOTSTRAP_KEY``, ``SBX_BASIC_*``, ``SBX_ACCOUNT_CREDENTIAL*``,
# ``SBX_LINEAR_API_KEY``, ``CODEX_AUTH_JSON``) travels exclusively through
# ``modal.Secret`` mounts and must never be baked into a function env. The
# keys below are names/tunables only — safe to record in the deployment.
_PROVIDER_SEED_PROVIDERS = ("CODEX", "DEVIN", "ANTIGRAVITY", "GROK", "OPENCODE")
_PROVIDER_SEED_SUFFIXES = ("ACCOUNT_ID", "SECRET_NAME", "SLOTS", "MODELS", "ACCOUNTS")

REMOTE_ENV_KEYS: tuple[str, ...] = (
    "SBX_MODAL_APP_NAME",
    "SBX_SESSIONS_DICT",
    "SBX_RUNS_DICT",
    "SBX_ACCOUNTS_DICT",
    "SBX_WORKFLOWS_DICT",
    "SBX_ARTIFACTS_DICT",
    "SBX_WORKSPACES_DICT",
    "SBX_IMAGE_CODEX",
    "SBX_IMAGE_DEVIN",
    "SBX_IMAGE_ANTIGRAVITY",
    "SBX_IMAGE_GROK",
    "SBX_IMAGE_OPENCODE",
    "SBX_CODEX_SECRET_NAME",
    "SBX_BASIC_SECRET_NAME",
    "SBX_V1_BOOTSTRAP_SECRET_NAME",
    "SBX_ACCOUNT_SECRET_PREFIX",
    "SBX_MAX_CONCURRENT",
    "SBX_IDLE_TIMEOUT_S",
    "SBX_DEFAULT_MODEL",
    "SBX_SSE_KEEPALIVE_SECONDS",
    "SBX_DEVIN_BURST_SLOTS",
    "SBX_RUNNER_CMD",
    "SBX_DEVIN_TRANSPORT",
    "SBX_GITHUB_EPHEMERAL",
    "SBX_LINEAR_MCP_EPHEMERAL",
    *(
        f"SBX_{provider}_{suffix}"
        for provider in _PROVIDER_SEED_PROVIDERS
        for suffix in _PROVIDER_SEED_SUFFIXES
    ),
)


def remote_env_overlay(
    env: Mapping[str, str] | None = None, *, app_name: str | None = None
) -> dict[str, str]:
    """Deploy-time env the remote control functions must see.

    ``control/modal_app.py`` bakes this into ``@app.function(env=...)`` so a
    deploy configured with non-default Dict/Secret/image names (a parallel RC
    deployment) actually uses them remotely instead of silently falling back
    to the production contract names. ``SBX_MODAL_APP_NAME`` is always set —
    ``ModalBackend`` scopes ``Sandbox.create`` to it, so an unset value would
    attach the deployment's sandboxes to the production ``sbx-control`` app.
    """
    env = os.environ if env is None else env
    out = {key: env[key] for key in REMOTE_ENV_KEYS if env.get(key)}
    out["SBX_MODAL_APP_NAME"] = app_name or env.get("SBX_MODAL_APP_NAME") or MODAL_APP_NAME
    return out


def default_runner_cmd(*, backend_kind: str) -> list[str]:
    override = os.environ.get("SBX_RUNNER_CMD")
    if override:
        import shlex

        return shlex.split(override)
    if backend_kind != "modal":
        try:
            import runtime.runner  # noqa: F401
        except ImportError:
            stub = Path(__file__).resolve().parents[1] / "tests" / "fakes" / "stub_runner.py"
            if stub.is_file():
                return [sys.executable, str(stub)]
        return [sys.executable, "-m", "runtime.runner"]
    return ["python", "-m", "runtime.runner"]

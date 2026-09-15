"""Control-plane constants. Values come from contracts + P0 (SOR-28)."""

from __future__ import annotations

import os
import sys
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

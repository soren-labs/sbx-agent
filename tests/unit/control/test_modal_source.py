"""Static checks: ModalBackend / modal_app match P0 signatures without importing modal."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODAL_BACKEND = ROOT / "control" / "backends" / "modal.py"
MODAL_APP = ROOT / "control" / "modal_app.py"


def test_modal_backend_source_matches_p0() -> None:
    src = MODAL_BACKEND.read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports = [
        node.names[0].name if isinstance(node, ast.Import) else node.module
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert "modal" not in imports
    assert not any(name == "modal" or (name or "").startswith("modal.") for name in imports)

    assert 'Sandbox.create(\n            "sleep",\n            "infinity"' in src or (
        '"sleep"' in src and '"infinity"' in src and "Sandbox.create" in src
    )
    assert "App.lookup" in src
    assert "create_if_missing=True" in src
    assert "bufsize=1" in src
    assert "write_eof" in src
    # SOR-181: create/restore take the spec's declared compute, falling
    # back to the deployment constants when the spec carries none.
    assert "spec.cpu" in src or "cpu=CPU" in src or "cpu=(1, 2)" in src
    assert "spec.memory_mib" in src or "memory=MEMORY_MIB" in src or "memory=(1024, 4096)" in src
    # SOR-132/SOR-134 + SOR-135: the native timers resolve from the shared
    # lifecycle chain so operator overrides reach the sandbox, not just the
    # reaper — and the native idle bound is the dedicated
    # ``sandbox_idle_timeout_s`` knob, never the post-session retention.
    assert "timeout=lifecycle.sandbox_timeout_s" in src
    assert "idle_timeout=lifecycle.sandbox_idle_timeout_s" in src
    assert "lifecycle_config" in src
    assert "workdir=WORK_DIR" in src or 'workdir="/work"' in src
    assert "CODEX_HOME" in src
    assert "SBX_WORK" in src
    assert "sbx-codex-auth" in src or "CODEX_SECRET_NAME" in src
    assert "Secret.from_dict" in src
    assert "CODEX_AUTH_JSON" in src
    assert "Sandbox.from_id" in src
    assert "sb.terminate()" in src or ".terminate()" in src
    assert "sb.poll()" in src or ".poll()" in src
    assert "Sandbox.list" in src
    assert "tags=" in src
    assert "secrets=" in src
    assert "ConflictError" in src
    assert "NotFoundError" in src
    assert "_codex_secrets" in src
    assert "from_name" in src


def test_modal_app_source_has_decorators() -> None:
    src = MODAL_APP.read_text(encoding="utf-8")
    assert "@modal.asgi_app()" in src
    assert "@modal.concurrent(max_inputs=20)" in src
    assert 'modal.Cron("*/5 * * * *")' in src
    # Secrets resolve via the provider-gated name list (SOR-115).
    assert "app_secret_names" in src
    assert "from control.app import create_app" in src
    assert "CONTROL_IMAGE" in src
    assert "image=CONTROL_IMAGE" in src
    assert src.count("image=CONTROL_IMAGE") >= 2
    assert "debian_slim" in src
    assert "pip_install" in src
    for pkg in ("fastapi", "httpx", "pydantic", "uvicorn", "anyio", "starlette"):
        assert pkg in src


def test_modal_app_deploy_source_closure_includes_runtime() -> None:
    """SOR-138: the deploy image must carry the ``runtime`` package.

    Modal's entrypoint mount ships only the function's own top-level package
    (``control``); ``control.app`` imports ``runtime.runner.*`` transitively
    and ``control/backends/modal.py`` lazily imports ``runtime.image``, so a
    deploy that omits ``runtime`` cannot start.
    """
    src = MODAL_APP.read_text(encoding="utf-8")
    assert 'add_local_python_source("runtime")' in src


def test_control_app_import_does_not_load_modal(monkeypatch) -> None:
    for name in list(sys.modules):
        if name == "modal" or name.startswith("modal."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, "control.app", raising=False)
    monkeypatch.delitem(sys.modules, "control.modal_app", raising=False)
    monkeypatch.delitem(sys.modules, "control.backends.modal", raising=False)
    monkeypatch.setenv("SBX_BACKEND", "local")
    import control.app as app_mod

    assert app_mod.create_app is not None
    assert "modal" not in sys.modules
    assert "control.modal_app" not in sys.modules
    assert "control.backends.modal" not in sys.modules


def test_cost_is_sandbox_duration_times_unit_price() -> None:
    from control.config import SANDBOX_USD_PER_S
    from control.service import cost_estimate_usd

    assert SANDBOX_USD_PER_S > 0
    # 1 core + 1 GiB request floor (P0 ≈ $0.166/h)
    hourly = SANDBOX_USD_PER_S * 3600
    assert 0.15 < hourly < 0.18
    assert cost_estimate_usd(0) == 0
    assert cost_estimate_usd(10) == round(10 * SANDBOX_USD_PER_S, 6)

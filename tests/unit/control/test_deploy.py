"""control.deploy.deploy() is the make-deploy entry; it must not parse argv."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import control.deploy as deploy_mod
import pytest


def _console_dist_fixture(tmp_path: Path) -> Path:
    dist = tmp_path / "console-dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        '<html><body><div id="root"></div></body></html>', encoding="utf-8"
    )
    return dist


def test_deploy_invokes_modal_cli(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []

    def fake_check_call(cmd: list[str]) -> int:
        calls.append(list(cmd))
        return 0

    # SOR-266 preflight: the unit test stands in a prebuilt dist rather
    # than depending on a real ``npm run build`` artifact in the checkout.
    monkeypatch.setenv("SBX_CONSOLE_DIST", str(_console_dist_fixture(tmp_path)))
    monkeypatch.setattr(deploy_mod.subprocess, "check_call", fake_check_call)
    deploy_mod.deploy()
    assert len(calls) == 1
    argv = calls[0]
    assert argv[1:4] == ["-m", "modal", "deploy"]
    assert "-m" in argv[4:]
    assert "control.modal_app" in argv
    assert "control.app" not in argv


def test_deploy_fails_loudly_without_console_build(monkeypatch, tmp_path) -> None:
    """SOR-266: a real deploy must never silently ship a root that has no
    V2 console — a missing artifact aborts before ``modal deploy`` runs."""
    calls: list[list[str]] = []
    monkeypatch.setattr(deploy_mod.subprocess, "check_call", lambda cmd: calls.append(list(cmd)))
    monkeypatch.setenv("SBX_CONSOLE_DIST", str(tmp_path / "missing-dist"))
    with pytest.raises(SystemExit, match="console build artifact missing"):
        deploy_mod.deploy()
    assert not calls


def test_import_deploy_ignores_pytest_argv(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["pytest", "tests/unit/control", "-k", "deploy"])
    importlib.reload(deploy_mod)
    assert callable(deploy_mod.deploy)

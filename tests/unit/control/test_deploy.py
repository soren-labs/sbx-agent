"""control.deploy.deploy() is the make-deploy entry; it must not parse argv."""

from __future__ import annotations

import importlib
import sys

import control.deploy as deploy_mod


def test_deploy_invokes_modal_cli(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_check_call(cmd: list[str]) -> int:
        calls.append(list(cmd))
        return 0

    monkeypatch.setattr(deploy_mod.subprocess, "check_call", fake_check_call)
    deploy_mod.deploy()
    assert len(calls) == 1
    argv = calls[0]
    assert argv[1:4] == ["-m", "modal", "deploy"]
    assert "-m" in argv[4:]
    assert "control.modal_app" in argv
    assert "control.app" not in argv


def test_import_deploy_ignores_pytest_argv(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["pytest", "tests/unit/control", "-k", "deploy"])
    importlib.reload(deploy_mod)
    assert callable(deploy_mod.deploy)

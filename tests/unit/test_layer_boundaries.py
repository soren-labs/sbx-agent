"""Dependency direction rules for the unified packages (RFC 09)."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _imports(package: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in (ROOT / package).rglob("*.py"):
        names: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        found[str(path.relative_to(ROOT))] = names
    return found


def _violations(package: str, forbidden: tuple[str, ...]) -> list[str]:
    out = []
    for path, names in _imports(package).items():
        for name in names:
            if any(name == f or name.startswith(f + ".") for f in forbidden):
                out.append(f"{path} imports {name}")
    return out


def test_domain_is_pure() -> None:
    assert (
        _violations(
            "control/domain",
            (
                "psycopg",
                "fastapi",
                "modal",
                "httpx",
                "runtime",
                "control.persistence",
                "control.application",
                "control.api",
                "control.jobs",
            ),
        )
        == []
    )


def test_application_depends_on_ports_not_infrastructure() -> None:
    assert (
        _violations(
            "control/application",
            (
                "psycopg",
                "fastapi",
                "modal",
                "httpx",
                "control.persistence",
                "control.api",
                "control.executors",
                "control.integrations",
                "runtime",
            ),
        )
        == []
    )


def test_protocol_is_data_only() -> None:
    assert _violations("protocol", ("control", "runtime", "psycopg", "fastapi", "modal")) == []


def test_runtime_never_imports_control() -> None:
    for package in (
        "runtime/daemon",
        "runtime/harnesses",
        "runtime/security",
        "runtime/subscriptions",
    ):
        assert _violations(package, ("control", "psycopg", "fastapi", "modal")) == []


def test_executors_do_not_import_harness_adapters() -> None:
    assert _violations("control/executors", ("runtime.harnesses", "runtime.daemon")) == []
    assert _violations("control/runtime_client", ("runtime",)) == []
    assert _violations("runtime/harnesses", ("modal", "control.executors")) == []

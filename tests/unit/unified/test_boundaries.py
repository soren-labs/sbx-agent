import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def imports(file):
    for node in ast.walk(ast.parse(file.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_domain_has_no_io_or_legacy_authority():
    forbidden = (
        "fastapi",
        "psycopg",
        "httpx",
        "modal",
        "runtime",
        "control.persistence",
        "control.jobs",
    )
    for file in (ROOT / "control/domain").rglob("*.py"):
        assert not any(module.startswith(forbidden) for module in imports(file)), file


def test_control_does_not_import_harness_or_daemon_implementation():
    for file in (ROOT / "control").rglob("*.py"):
        assert not any(
            module.startswith(("runtime.harnesses", "runtime.daemon")) for module in imports(file)
        ), file
    for file in (ROOT / "runtime/harnesses").rglob("*.py"):
        assert not any(
            module.startswith(("control", "modal", "psycopg")) for module in imports(file)
        ), file


def test_retired_roots_are_absent_and_no_facade_reads():
    for name in (
        "control/api_v1",
        "control/api_v2",
        "control/hosted",
        "control/store.py",
        "control/service.py",
        "runtime/runner",
        "broker",
        "web",
        "src/sbx/sdk/models.py",
    ):
        assert not (ROOT / name).exists(), name
    for file in (ROOT / "control").rglob("*.py"):
        assert "control_records" not in file.read_text(), file
    assert not any((ROOT / "docs/specs").glob("*.yaml"))

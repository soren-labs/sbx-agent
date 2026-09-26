"""Plumbing that keeps code examples in the public docs honest.

* Every ```python fenced block in ``docs-site/src/content/docs`` must at least
  compile (``textwrap.dedent`` applied first so list-indented snippets parse).
* Every repo-internal import in those snippets must resolve — ``sbx.*``,
  ``examples.*``, ``control.*``, ``runtime.*`` modules are checked with
  ``importlib.util.find_spec`` against the repo, not executed (no credentials,
  no network).
* Every ``examples/<file>.py`` path mentioned in prose must exist.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTENT = ROOT / "docs-site" / "src" / "content" / "docs"
SRC = ROOT / "src"

INTERNAL_TOP_LEVEL = {"sbx", "examples", "control", "runtime", "broker"}

FENCE = re.compile(r"```python\n(.*?)```", re.S)


def _blocks() -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for path in sorted(CONTENT.rglob("*")):
        if path.suffix not in (".md", ".mdx"):
            continue
        for i, m in enumerate(FENCE.finditer(path.read_text(encoding="utf-8"))):
            blocks.append((f"{path.relative_to(ROOT)}#{i}", m.group(1)))
    return blocks


BLOCKS = _blocks()


def _find_repo_spec(module: str) -> bool:
    """Resolve ``module`` by filesystem layout under src/ and the repo root.

    File-based (not ``find_spec``) so namespace-package dirs like ``examples/``
    resolve even when an unrelated site-packages distribution shadows the name.
    """
    parts = module.split(".")
    for base in (SRC, ROOT):
        path = base
        ok = True
        for i, part in enumerate(parts):
            if (path / part).is_dir():
                path = path / part
            elif (path / f"{part}.py").is_file():
                if i != len(parts) - 1:
                    ok = False
                    break
                path = path / f"{part}.py"
            else:
                ok = False
                break
        if ok:
            return True
    return False


@pytest.mark.parametrize("label,body", BLOCKS, ids=[b[0] for b in BLOCKS])
def test_python_block_compiles(label: str, body: str) -> None:
    src = textwrap.dedent(body)
    compile(src, label, "exec")  # SyntaxError fails the test


@pytest.mark.parametrize("label,body", BLOCKS, ids=[b[0] for b in BLOCKS])
def test_python_block_imports_resolve(label: str, body: str) -> None:
    tree = ast.parse(textwrap.dedent(body))
    missing: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [node.module] if node.module and node.level == 0 else []
        else:
            continue
        for mod in mods:
            top = mod.split(".")[0]
            if top in INTERNAL_TOP_LEVEL and not _find_repo_spec(mod):
                missing.append(mod)
    assert not missing, f"{label}: unresolved repo imports {missing}"


def test_referenced_example_files_exist() -> None:
    missing: list[str] = []
    for path in sorted(CONTENT.rglob("*")):
        if path.suffix not in (".md", ".mdx"):
            continue
        for m in re.finditer(r"examples/[\w./-]+\.py", path.read_text(encoding="utf-8")):
            if not (ROOT / m.group(0)).exists():
                missing.append(f"{path.name}: {m.group(0)}")
    assert not missing, f"docs reference missing example files: {missing}"

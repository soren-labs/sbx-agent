"""Public-docs foundation checks (SOR-234).

The public site (``docs-site/``) must stay aligned with the runtime sources of
truth and must not leak internal tracking language. These tests cover:

* the generated error-catalog table in ``reference/errors.md`` — kept in sync
  by ``docs-site/scripts/sync_error_reference.py``;
* the run-error table in the same page — every code/source is canonical;
* no ``SOR-*``/issue-tracker language anywhere in public docs content;
* ``public/llms.txt`` — every local link resolves to a real page or asset;
* the astro config exposes the required public IA sections.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS_SITE = ROOT / "docs-site"
CONTENT = DOCS_SITE / "src" / "content" / "docs"
PUBLIC = DOCS_SITE / "public"
ERRORS_PAGE = CONTENT / "reference" / "errors.md"
CONFIG = DOCS_SITE / "astro.config.mjs"
LLMS = PUBLIC / "llms.txt"

DOC_FILES = sorted(p for p in CONTENT.rglob("*") if p.suffix in (".md", ".mdx") and p.is_file())


def _slugs() -> set[str]:
    """All page slugs for the default locale (index files map to the dir)."""
    slugs = {"index"}
    for path in DOC_FILES:
        rel = path.relative_to(CONTENT).with_suffix("")
        parts = rel.parts
        if parts[0] == "zh-cn":
            continue
        slug = "/".join(parts)
        slugs.add(re.sub(r"/index$", "", slug))
    return slugs


def _local_target_exists(url_path: str) -> bool:
    path = url_path.split("#")[0].strip("/")
    if not path:
        return True
    if path in _slugs():
        return True
    if (PUBLIC / path).exists():
        return True
    # generated at build time by scripts/prepare-openapi.mjs
    if path in ("openapi.json",):
        return True
    # starlight-openapi generates pages under /reference/api/
    if path == "reference/api" or path.startswith("reference/api/"):
        return True
    return False


def test_error_catalog_table_in_sync() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(DOCS_SITE / "scripts" / "sync_error_reference.py"),
            "--check",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def _table_codes(section: str) -> dict[str, str]:
    """Parse ``| `code` | source |`` rows out of a markdown section."""
    rows: dict[str, str] = {}
    for line in section.splitlines():
        m = re.match(r"\|\s*`([a-z_]+)`\s*\|\s*`?([a-z_]+)`?\s*\|", line)
        if m:
            rows[m.group(1)] = m.group(2)
    return rows


def test_http_table_uses_canonical_codes() -> None:
    from control.api_v1.error_catalog import ERROR_CATALOG

    text = ERRORS_PAGE.read_text(encoding="utf-8")
    block = text.split("BEGIN GENERATED: http-error-catalog", 1)[1].split(
        "END GENERATED: http-error-catalog", 1
    )[0]
    documented = {
        m.group(2): m.group(1) for m in re.finditer(r"\|\s*(\d{3})\s*\|\s*`([a-z_]+)`\s*\|", block)
    }
    canonical = {spec.code: str(spec.status) for spec in ERROR_CATALOG.values()}
    assert set(documented) == set(canonical), (
        f"missing: {sorted(set(canonical) - set(documented))}, "
        f"extra: {sorted(set(documented) - set(canonical))}"
    )
    mismatched = {
        c: (documented[c], canonical[c]) for c in documented if documented[c] != canonical[c]
    }
    assert not mismatched, f"status drift: {mismatched}"


def test_run_error_table_matches_canonical_codes() -> None:
    from control.run_errors import RUN_ERROR_CODES, RUN_ERROR_SOURCES

    text = ERRORS_PAGE.read_text(encoding="utf-8")
    run_section = text.split("## Run error codes", 1)[1].split("\n## ", 1)[0]
    documented = _table_codes(run_section)
    assert set(documented) == set(RUN_ERROR_CODES), (
        f"missing: {sorted(set(RUN_ERROR_CODES) - set(documented))}, "
        f"extra: {sorted(set(documented) - set(RUN_ERROR_CODES))}"
    )
    bad_sources = set(documented.values()) - set(RUN_ERROR_SOURCES)
    assert not bad_sources, f"non-canonical sources: {sorted(bad_sources)}"


@pytest.mark.parametrize("page", DOC_FILES, ids=lambda p: str(p.relative_to(CONTENT)))
def test_no_internal_tracker_language(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    hits = re.findall(r"\b[A-Z]{2,}-\d+\b", text)
    assert not hits, f"{page.name}: tracker references {sorted(set(hits))}"


def test_llms_txt_links_resolve() -> None:
    assert LLMS.exists(), "public/llms.txt missing"
    links = re.findall(r"\]\(([^)]+)\)", LLMS.read_text(encoding="utf-8"))
    assert links, "llms.txt lists no links"
    bad = [
        link
        for link in links
        if not re.match(r"^(https?:)?//", link) and not _local_target_exists(link)
    ]
    assert not bad, f"llms.txt unresolved links: {bad}"


def test_public_ia_sections_present() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    for label in (
        "Getting started",
        "Guides",
        "Concepts",
        "API & SDK reference",
        "Integrations",
        "Self-hosting",
        "Troubleshooting",
        "For agents",
    ):
        assert f"label: '{label}'" in config, f"sidebar missing section {label}"

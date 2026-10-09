"""Docs-site foundation checks for the unified SBX documentation (offline, fast).

The site in ``docs-site/`` must describe only the unified architecture: the
required pages exist, maintained content carries no legacy product vocabulary,
internal links resolve, the error table and the configuration page match the
code, and the generated public reference files are present and correct.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from protocol.errors import ERROR_CODES

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "docs-site"
CONTENT = SITE / "src" / "content" / "docs"
PUBLIC = SITE / "public"
CONFIG = SITE / "astro.config.mjs"

REQUIRED_PAGES = [
    "index.mdx",
    "getting-started/introduction.md",
    "getting-started/quick-start.md",
    "concepts/index.md",
    "guides/connections.md",
    "guides/sessions-and-turns.md",
    "guides/changes-and-delivery.md",
    "guides/child-sessions.md",
    "guides/console.md",
    "api/overview.md",
    "api/events.md",
    "api/errors.md",
    "sdk/python.md",
    "reference/cli.md",
    "reference/providers.md",
    "self-hosting/configuration.md",
    "self-hosting/security.md",
    "project/changelog.md",
    "project/contributing.md",
    "troubleshooting/index.md",
]

LEGACY_TERMS = [
    r"/v[12]\b",
    r"(?i)\btasks?\b",
    r"(?i)\bagent runs?\b",
    r"(?i)(?<!self-)\bhosted\b",
    r"(?i)\bworkflows?\b",
    r"(?i)\bhand-?offs?\b",
    r"(?i)\bartifacts?\b",
    r"(?i)\baccount pools?\b",
    r"(?i)\bbasic auth",
    r"\bsbx (?:deploy|doctor|smoke|upgrade)\b",
    r"zh-cn",
    r"\bSOR-\d+",
]

DOC_FILES = sorted(p for p in CONTENT.rglob("*") if p.suffix in (".md", ".mdx"))
MAINTAINED = [*DOC_FILES, PUBLIC / "llms.txt", CONFIG]


def _slugs() -> set[str]:
    slugs = {"index"}
    for path in DOC_FILES:
        slug = "/".join(path.relative_to(CONTENT).with_suffix("").parts)
        slugs.add(re.sub(r"/index$", "", slug))
    return slugs


def _resolves(url: str) -> bool:
    path = url.split("#")[0].strip("/")
    return (
        not path
        or path in _slugs()
        or (PUBLIC / path).exists()
        or path == "openapi.json"
        or path == "reference/api"
        or path.startswith("reference/api/")
    )


@pytest.mark.parametrize("page", REQUIRED_PAGES)
def test_required_page_exists(page: str) -> None:
    assert (CONTENT / page).is_file(), page


def test_english_only_site() -> None:
    assert not (CONTENT / "zh-cn").exists()
    assert not (PUBLIC / "zh-cn").exists()
    config = CONFIG.read_text()
    assert "locales" not in config
    assert "translations" not in config


@pytest.mark.parametrize("path", MAINTAINED, ids=lambda p: str(p.relative_to(SITE)))
def test_no_legacy_vocabulary(path: Path) -> None:
    # The Anthropic Messages request path belongs to the model provider, not to SBX's API.
    text = path.read_text().replace("{base_url}/v1/messages", "")
    for pattern in LEGACY_TERMS:
        match = re.search(pattern, text)
        assert match is None, f"{path.relative_to(SITE)}: legacy term {match.group(0)!r}"


def test_internal_links_resolve() -> None:
    link = re.compile(r"\]\(\s*(/[^)\s]+)\s*\)|src=\"(/[^\"]+)\"")
    for path in [*DOC_FILES, PUBLIC / "llms.txt"]:
        for match in link.finditer(path.read_text()):
            target = match.group(1) or match.group(2)
            assert _resolves(target), f"{path.relative_to(SITE)}: unresolved link {target}"


def test_sidebar_slugs_and_redirects_point_at_pages() -> None:
    config = CONFIG.read_text()
    for items in re.findall(r"items:\s*\[([^\]]*)\]", config, flags=re.S):
        for slug in re.findall(r"'([a-z0-9-]+(?:/[a-z0-9-]+)*)'", items):
            assert slug in _slugs(), f"sidebar slug without page: {slug}"
    redirects = re.search(r"redirects:\s*\{([^}]*)\}", config, flags=re.S)
    assert redirects
    for _, target in re.findall(r"'([^']+)':\s*'([^']+)'", redirects.group(1)):
        assert _resolves(target), f"redirect to missing page: {target}"


def test_openapi_pipeline_uses_unified_spec() -> None:
    script = (SITE / "scripts" / "prepare-openapi.mjs").read_text()
    assert "docs/specs/unified/openapi.yaml" in script
    assert "api_v1" not in script and "api-v1" not in script
    assert "execFileSync" not in script
    config = CONFIG.read_text()
    assert "REST API (/api)" in config
    assert ".generated/openapi.yaml" in config
    assert (ROOT / "docs" / "specs" / "unified" / "openapi.yaml").is_file()


def test_error_table_matches_canonical_vocabulary() -> None:
    text = (CONTENT / "api" / "errors.md").read_text()
    rows = re.findall(r"^\| `([a-z_]+)` \| (\w+) \| (\d+) \| (yes|no) \|$", text, flags=re.M)
    documented = {code: (cat, int(status), retry == "yes") for code, cat, status, retry in rows}
    assert documented == dict(ERROR_CODES)


def test_configuration_page_lists_every_control_plane_variable() -> None:
    composition = (ROOT / "control" / "composition.py").read_text()
    variables = set(re.findall(r"\"(SBX_[A-Z_]+)\"", composition))
    assert {"SBX_DATABASE_URL", "SBX_VAULT_KEYS", "SBX_RUNTIME_MASTER_KEY"} <= variables
    page = (CONTENT / "self-hosting" / "configuration.md").read_text()
    missing = sorted(name for name in variables if f"`{name}`" not in page)
    assert not missing, f"undocumented variables: {missing}"


def test_quick_start_covers_minimum_setup() -> None:
    text = (CONTENT / "getting-started" / "quick-start.md").read_text()
    for needle in (
        "sbx serve",
        "PostgreSQL",
        "Modal",
        "GitHub",
        "Inference API key",
        "Claude Code",
    ):
        assert needle in text, needle
    assert "OpenCode Zen" not in text and "auth.json" not in text


def test_generated_cli_help() -> None:
    text = (PUBLIC / "cli-help.txt").read_text()
    for command in ("auth", "connections", "sessions", "execute", "serve", "migrate"):
        assert f"===== sbx {command} =====" in text, command
    assert "sbx deploy" not in text


def test_generated_config_reference() -> None:
    data = json.loads((PUBLIC / "config-reference.json").read_text())
    assert {row["field"] for row in data} == {"base_url", "api_key", "workspace_id"}


def test_generated_provider_reference() -> None:
    data = json.loads((PUBLIC / "provider-reference.json").read_text())
    tiers = {row["provider"]: row["support"] for row in data}
    assert tiers == {
        "opencode": "supported",
        "codex": "supported",
        "claude": "supported",
        "grok": "supported",
        "commandcode": "supported",
        "devin": "disabled",
        "antigravity": "disabled",
    }


def test_example_exists() -> None:
    assert (ROOT / "examples" / "unified_mvp.py").is_file()

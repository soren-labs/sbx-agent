"""Regenerate the HTTP error-code table in the docs errors reference.

``control/api_v1/error_catalog.py`` is the canonical source: every code the
``/v1`` API may emit is a row there. This script renders that catalog into the
``<!-- BEGIN/END GENERATED: http-error-catalog -->`` block of
``docs-site/src/content/docs/reference/errors.md``.

Usage:

    uv run python docs-site/scripts/sync_error_reference.py          # rewrite
    uv run python docs-site/scripts/sync_error_reference.py --check  # drift check

``--check`` exits non-zero when the committed page is stale, so CI and the
docs-foundation test can enforce parity without a node build.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from control.api_v1.error_catalog import ERROR_ACTIONS, ERROR_CATALOG  # noqa: E402

PAGE = ROOT / "docs-site" / "src" / "content" / "docs" / "reference" / "errors.md"
BEGIN = "<!-- BEGIN GENERATED: http-error-catalog -->"
END = "<!-- END GENERATED: http-error-catalog -->"


def render() -> str:
    lines = [
        BEGIN,
        "",
        "Every code the API can emit, generated from the runtime catalog.",
        "",
        "| Status | Code | Retryable | Client action | Description |",
        "| --- | --- | --- | --- | --- |",
    ]
    for spec in sorted(ERROR_CATALOG.values(), key=lambda s: (s.status, s.code)):
        lines.append(
            f"| {spec.status} | `{spec.code}` | "
            f"{'yes' if spec.retryable else 'no'} | `{spec.action}` | "
            f"{spec.description} |"
        )
    lines += [
        "",
        "`Client action` is the stable machine-readable hint sent in every error "
        "body: " + ", ".join(f"`{a}`" for a in ERROR_ACTIONS) + ".",
        "",
        END,
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    text = PAGE.read_text(encoding="utf-8")
    start = text.index(BEGIN)
    end = text.index(END) + len(END)
    updated = text[:start] + render() + text[end:]
    if "--check" in argv:
        if updated != text:
            print(
                f"{PAGE}: error catalog table is stale — "
                "run `uv run python docs-site/scripts/sync_error_reference.py`",
                file=sys.stderr,
            )
            return 1
        print("sync_error_reference: errors.md is in sync")
        return 0
    if updated == text:
        print("sync_error_reference: already in sync")
        return 0
    PAGE.write_text(updated, encoding="utf-8")
    print(f"sync_error_reference: rewrote {PAGE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

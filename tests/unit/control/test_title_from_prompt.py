from __future__ import annotations

from control.service import TITLE_MAX_CHARS, title_from_prompt


def test_first_non_empty_line_with_markdown_stripped() -> None:
    assert title_from_prompt("\n\n# Add   dark mode\nmore") == "Add dark mode"


def test_empty_prompt_has_no_title() -> None:
    assert title_from_prompt(None) is None
    assert title_from_prompt(" \n\t\n") is None


def test_long_prompt_is_truncated() -> None:
    title = title_from_prompt("x" * 200)
    assert title is not None
    assert len(title) == TITLE_MAX_CHARS
    assert title.endswith("…")

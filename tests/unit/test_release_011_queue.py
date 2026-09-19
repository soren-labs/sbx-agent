from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "release_011_queue", ROOT / "scripts/release_011_queue.py"
)
assert SPEC and SPEC.loader
queue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue)


def test_queue_order_and_grouping() -> None:
    assert [x["key"] for x in queue.QUEUE] == [
        "wp-a",
        "wp-b",
        "wp-c",
        "wp-d",
        "wp-e",
        "wp-f",
        "cost",
        "wp-g",
        "wp-h1",
        "wp-i",
    ]
    assert queue.item("wp-e")["issues"] == ["SOR-132", "SOR-134"]
    assert queue.item("wp-f")["kind"] == "validation"
    assert queue.item("wp-i")["kind"] == "acceptance"


def test_next_key() -> None:
    assert queue.next_key("wp-a") == "wp-b"
    assert queue.next_key("wp-h1") == "wp-i"
    assert queue.next_key("wp-i") == ""


def test_author_prompt_contains_pr_marker_contract() -> None:
    prompt = queue.author_prompt(queue.item("wp-a"))
    assert "R011_TASK: wp-a" in prompt
    assert "Do not self-review" in prompt


def test_validation_prompt_contains_repository_dispatch_contract() -> None:
    prompt = queue.author_prompt(queue.item("wp-f"))
    assert "event_type=r011-complete" in prompt
    assert '"task_key":"wp-f"' in prompt


def test_acceptance_prompt_forbids_repairs() -> None:
    prompt = queue.author_prompt(queue.item("wp-i"))
    assert "Do not edit repository files" in prompt
    assert "Do not repair code" in prompt


def test_review_prompt_is_exact_head() -> None:
    sha = "a" * 40
    prompt = queue.reviewer_prompt("wp-a", 42, sha)
    assert sha in prompt
    assert "Do not modify code" in prompt
    assert f"R011_REVIEW: APPROVE sha={sha}" in prompt

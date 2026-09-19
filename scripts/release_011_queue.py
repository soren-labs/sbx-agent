#!/usr/bin/env python3
"""Release 0.1.1 Devin Cloud queue + session launcher.

This is intentionally small and release-specific. GitHub Actions is the state
machine; this file owns only the ordered work packages and safe Devin API call.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

REPO = "soren-labs/sbx-browser"

QUEUE: list[dict[str, object]] = [
    {
        "key": "wp-a",
        "issues": ["SOR-138"],
        "title": "WP-A Deploy Packaging",
        "kind": "author",
        "scope": (
            "Fix the control-plane deploy image/package closure so a fresh deploy includes "
            "runtime and no detached hotfix is required. Keep this PR limited to SOR-138."
        ),
    },
    {
        "key": "wp-b",
        "issues": ["SOR-139"],
        "title": "WP-B Run Finalization",
        "kind": "author",
        "scope": (
            "Fix provider success -> durable FINISHED / agent idle / publish-ready reconciliation. "
            "Do not mix artifact-store or timeout configuration changes."
        ),
    },
    {
        "key": "wp-c",
        "issues": ["SOR-140"],
        "title": "WP-C Evidence Artifact Store",
        "kind": "author",
        "scope": (
            "Fix bounded Modal-backed artifact list/query/member retrieval behavior. Preserve the "
            "already-working direct GET/create paths."
        ),
    },
    {
        "key": "wp-d",
        "issues": ["SOR-133"],
        "title": "WP-D GitHub Bridge Persistence",
        "kind": "author",
        "scope": (
            "Persist GitHub bridge enablement + secret-name through config/bootstrap/deploy/"
            "upgrade/doctor/status. Never persist or log the secret value."
        ),
    },
    {
        "key": "wp-e",
        "issues": ["SOR-132", "SOR-134"],
        "title": "WP-E Lifecycle Configuration",
        "kind": "author",
        "scope": (
            "Treat SOR-132 and SOR-134 as one configuration-chain fix/PR: turn max seconds, Modal "
            "native sandbox idle timeout, reaper grace, and resolved deploy config must agree. "
            "Do not fold the 5-minute post-session cost tuning from SOR-135 into running-sandbox "
            "idle."
        ),
    },
    {
        "key": "wp-f",
        "issues": ["SOR-122", "SOR-123"],
        "title": "WP-F Recovery Verification",
        "kind": "validation",
        "scope": (
            "Validation-only first. On latest main, verify SOR-122 PYTHONPATH normalization and "
            "run repeated worker-loss recovery checks for SOR-123. If both pass, make no code "
            "changes and dispatch r011-complete. If a bug still reproduces, make only the minimal "
            "fix in one PR."
        ),
    },
    {
        "key": "cost",
        "issues": ["SOR-135"],
        "title": "Post-blocker idle cost tuning",
        "kind": "author",
        "scope": (
            "After blockers are fixed, reduce post-session dev-cloud idle retention from 30 "
            "minutes toward the agreed 5-minute default without confusing it with the native "
            "running sandbox idle timeout. Keep this PR limited to SOR-135."
        ),
    },
    {
        "key": "wp-g",
        "issues": ["SOR-144"],
        "title": "WP-G Release Hygiene",
        "kind": "author",
        "scope": (
            "Update CHANGELOG/version/tag naming, provider matrix evidence, README Quick Start, "
            "and document public /v1 versus internal legacy /api boundaries. No credential UX "
            "refactor."
        ),
    },
    {
        "key": "wp-h1",
        "issues": ["SOR-147"],
        "title": "WP-H1 OAuth credential auto-refresh write-back",
        "kind": "author",
        "scope": (
            "Implement automatic post-turn credential export/write-back, per-account Modal Secret "
            "refresh without redeploy, stale-writer/CAS protection, short refresh commit locking, "
            "one-shot auth_invalid self-heal, and zero secret leakage. Preserve official CLI auth "
            "files as the single source of truth."
        ),
    },
    {
        "key": "wp-i",
        "issues": ["SOR-146"],
        "title": "WP-I Final Clean-room Acceptance",
        "kind": "acceptance",
        "scope": (
            "Fresh Devin Cloud clean-room acceptance from a fresh clone of final main. Do not "
            "repair code. Cover init/config/provider import/deploy/doctor/smoke, real author "
            "workflow, Git/PR, structured reviewer, evidence artifact, recovery, cleanup, Devin "
            "and one multi-account provider. On failure report the blocker and stop."
        ),
    },
]


def item(key: str) -> dict[str, object]:
    for entry in QUEUE:
        if entry["key"] == key:
            return entry
    raise SystemExit(f"unknown release queue key: {key}")


def next_key(key: str) -> str:
    for idx, entry in enumerate(QUEUE):
        if entry["key"] == key:
            return str(QUEUE[idx + 1]["key"]) if idx + 1 < len(QUEUE) else ""
    raise SystemExit(f"unknown release queue key: {key}")


def author_prompt(entry: dict[str, object]) -> str:
    issues = ", ".join(entry["issues"])
    kind = entry["kind"]
    completion = ""
    if kind == "validation":
        completion = (
            "\nIf validation passes with no code changes, advance the chain with:\n"
            f"  gh api repos/{REPO}/dispatches -f event_type=r011-complete "
            f'-f client_payload=\'{{"task_key":"{entry["key"]}","status":"pass"}}\'\n'
            "If a bug reproduces and you need a PR instead, the PR body must contain the line "
            f"`R011_TASK: {entry['key']}`.\n"
        )
    elif kind == "acceptance":
        completion = (
            "\nThis is acceptance-only. Do not edit repository files or open a repair PR. "
            "Report PASS/FAIL with evidence.\n"
        )
    else:
        completion = (
            "\nCreate a dedicated branch, push it, and open exactly one PR against main. The PR "
            f"body must contain `R011_TASK: {entry['key']}`. Include tests and the canonical "
            f"issue(s): {issues}. Do not self-review the PR.\n"
        )

    return (
        f"Release 0.1.1 automated work package {entry['key']} — {entry['title']} ({issues}).\n\n"
        f"Scope: {entry['scope']}\n\n"
        "Repository: soren-labs/sbx-browser. Start from a fresh, up-to-date main checkout. "
        "You have GitHub and deployment credentials from the Devin Cloud environment. Never print "
        "secret values. Keep changes strictly within this work package. Run focused tests plus the "
        "relevant repository gates before reporting completion."
        f"{completion}"
    )


def reviewer_prompt(task_key: str, pr: int, sha: str) -> str:
    entry = item(task_key)
    issues = ", ".join(entry["issues"])
    return (
        f"Independent exact-head review for Release 0.1.1 task {task_key} ({issues}), PR #{pr}.\n\n"
        f"Review exactly commit {sha}; verify the PR head still equals that SHA before reviewing. "
        "Do not modify code. Inspect the diff and run focused tests as needed. If the exact head "
        "is acceptable, post a PR comment with exactly this first line:\n"
        f"R011_REVIEW: APPROVE sha={sha}\n"
        "If there is a blocking defect, post a PR comment with exactly this first line:\n"
        f"R011_REVIEW: REQUEST_CHANGES sha={sha}\n"
        "Then explain only concrete blocking findings below it. Do not approve a different SHA."
    )


def fixer_prompt(task_key: str, pr: int, sha: str) -> str:
    item(task_key)
    return (
        f"One allowed fixer pass for Release 0.1.1 task {task_key}, PR #{pr}, reviewed head {sha}. "
        "Read the latest independent reviewer comment, fix only those blocking findings on the "
        "existing PR branch, run focused tests, and push. Do not broaden scope and do not open a "
        "second PR."
    )


def create_session(prompt: str, title: str, tag: str) -> dict[str, str]:
    key = os.environ.get("DEVIN_API_KEY", "")
    if not key:
        raise SystemExit("DEVIN_API_KEY is required")
    if key.startswith("cog_"):
        raise SystemExit("release_011_queue.py currently requires a personal apk_* Devin API key")
    payload = json.dumps(
        {"prompt": prompt, "title": title[:100], "tags": ["release-0.1.1", tag]}
    ).encode()
    req = urllib.request.Request(
        "https://api.devin.ai/v1/sessions",
        data=payload,
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1000]
        raise SystemExit(f"Devin API HTTP {exc.code}: {detail}") from exc
    sid = str(body.get("session_id") or "")
    url = str(body.get("url") or "")
    if not url and sid:
        url = f"https://app.devin.ai/sessions/{sid.removeprefix('devin-')}"
    if not sid or not url:
        raise SystemExit("Devin API response missing session_id/url")
    return {"session_id": sid, "url": url}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show")
    show.add_argument("key")
    nxt = sub.add_parser("next")
    nxt.add_argument("key")
    launch = sub.add_parser("launch")
    launch.add_argument("--key", required=True)
    launch.add_argument("--mode", choices=["author", "review", "fixer"], default="author")
    launch.add_argument("--pr", type=int)
    launch.add_argument("--sha")
    args = parser.parse_args()

    if args.cmd == "show":
        print(json.dumps(item(args.key), ensure_ascii=False))
        return 0
    if args.cmd == "next":
        print(next_key(args.key))
        return 0

    entry = item(args.key)
    if args.mode == "author":
        prompt = author_prompt(entry)
        title = f"R011 {args.key}: {entry['title']}"
    else:
        if not args.pr or not args.sha:
            raise SystemExit("--pr and --sha are required for review/fixer")
        if args.mode == "review":
            prompt = reviewer_prompt(args.key, args.pr, args.sha)
            title = f"R011 review {args.key} PR #{args.pr}"
        else:
            prompt = fixer_prompt(args.key, args.pr, args.sha)
            title = f"R011 fixer {args.key} PR #{args.pr}"
    result = create_session(prompt, title, f"r011-{args.mode}-{args.key}")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

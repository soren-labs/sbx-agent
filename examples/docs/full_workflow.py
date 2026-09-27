"""Create → wait → deliver → review → merge using only the public SDK."""

from __future__ import annotations

import argparse
import json

from sbx.sdk import SbxClient


def run(client: SbxClient, prompt: str, repo: str) -> dict:
    created = client.tasks.create(
        prompt,
        source={"repo": repo},
        delivery={"pull_request": {}},
    )
    task = client.tasks.wait(created.task.id, timeout_s=1200)
    if task.status != "finished":
        raise RuntimeError(f"task did not finish successfully: {task.status}")
    revision = client.tasks.deliver(created.task.id)
    review = client.tasks.review(created.task.id, verdict="approve", revision=revision.id)
    merged = client.tasks.merge(created.task.id, revision=revision.id)
    return {
        "task": task.raw,
        "revision": revision.raw,
        "review": review.raw,
        "merged": merged.raw,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo")
    parser.add_argument("prompt")
    args = parser.parse_args()
    with SbxClient() as client:
        print(json.dumps(run(client, args.prompt, args.repo), indent=2))


if __name__ == "__main__":
    main()

"""Record an exact-revision review verdict."""

from __future__ import annotations

import argparse
import json

from sbx.sdk import SbxClient


def run(client: SbxClient, task_id: str, verdict: str, revision: str = "latest") -> dict:
    return client.tasks.review(task_id, verdict=verdict, revision=revision).raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--revision", default="latest")
    parser.add_argument(
        "--verdict", choices=("approve", "request_changes", "comment"), default="approve"
    )
    args = parser.parse_args()
    with SbxClient() as client:
        print(json.dumps(run(client, args.task_id, args.verdict, args.revision), indent=2))


if __name__ == "__main__":
    main()

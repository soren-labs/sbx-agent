"""Request the review-gated merge of a delivered revision."""

from __future__ import annotations

import argparse
import json

from sbx.sdk import SbxClient


def run(client: SbxClient, task_id: str, revision: str = "latest") -> dict:
    return client.tasks.merge(task_id, revision=revision).raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--revision", default="latest")
    args = parser.parse_args()
    with SbxClient() as client:
        print(json.dumps(run(client, args.task_id, args.revision), indent=2))


if __name__ == "__main__":
    main()

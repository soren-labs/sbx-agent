"""Deliver the latest durable revision as the task's declared branch/PR."""

from __future__ import annotations

import argparse
import json

from sbx.sdk import SbxClient


def run(client: SbxClient, task_id: str) -> dict:
    return client.tasks.deliver(task_id).raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    args = parser.parse_args()
    with SbxClient() as client:
        print(json.dumps(run(client, args.task_id), indent=2))


if __name__ == "__main__":
    main()

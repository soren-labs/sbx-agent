"""Watch the current run for a task, then print the durable task state."""

from __future__ import annotations

import argparse
import json

from sbx.sdk import SbxClient


def run(client: SbxClient, task_id: str) -> dict:
    detail = client.tasks.get(task_id)
    if detail.agent is not None and detail.run is not None:
        for event in client.runs.watch(detail.agent.id, detail.run.id):
            print(event.type, json.dumps(event.data, default=str))
    return client.tasks.get(task_id).raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    args = parser.parse_args()
    with SbxClient() as client:
        print(json.dumps(run(client, args.task_id), indent=2))


if __name__ == "__main__":
    main()

"""Send a follow-up to an existing task and wait for that run."""

from __future__ import annotations

import argparse
import json

from sbx.sdk import SbxClient


def run(client: SbxClient, task_id: str, prompt: str) -> dict:
    detail = client.tasks.get(task_id)
    if detail.agent is None:
        raise RuntimeError("task has no live/resumable agent")
    followup = client.tasks.followup(task_id, prompt)
    terminal = client.runs.wait(detail.agent.id, followup.id)
    return terminal.raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("prompt")
    args = parser.parse_args()
    with SbxClient() as client:
        print(json.dumps(run(client, args.task_id, args.prompt), indent=2))


if __name__ == "__main__":
    main()

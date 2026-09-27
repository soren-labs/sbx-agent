"""Create one task using the high-level Task API."""

from __future__ import annotations

import argparse
import json

from sbx.sdk import SbxClient, TaskCreated


def run(client: SbxClient, prompt: str, repo: str | None = None) -> TaskCreated:
    source = {"repo": repo} if repo else None
    delivery = {"pull_request": {}} if repo else None
    return client.tasks.create(prompt, source=source, delivery=delivery)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--repo")
    args = parser.parse_args()
    with SbxClient() as client:
        created = run(client, args.prompt, args.repo)
        print(json.dumps(created.raw, indent=2))


if __name__ == "__main__":
    main()

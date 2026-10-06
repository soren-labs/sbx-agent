"""Minimal unified SDK walkthrough (no secrets in argv; values come from the environment).

SBX_BASE_URL=... SBX_API_KEY=... python examples/unified_mvp.py owner/repo
"""

from __future__ import annotations

import os
import sys

from sbx import SBXClient


def main(repo: str) -> None:
    client = SBXClient(
        os.environ.get("SBX_BASE_URL", "http://127.0.0.1:8800"), os.environ["SBX_API_KEY"]
    )
    print("preferred model:", client.models()["preferred_model"])
    result = client.execute(
        "Add a CONTRIBUTING.md with one paragraph.",
        repository={"full_name": repo},
        executor={"backend": "modal"},
    )
    session_id, turn = result["session_id"], result["turn"]
    print("turn:", turn["state"])
    changeset = client.changesets.wait_ready(session_id, source_turn_id=turn["id"])
    review = client.delegations.spawn(session_id, "review", changeset_id=changeset["id"])
    print("review:", client.delegations.wait_result(review["delegation_id"])["result"])
    delivery = client.deliveries.wait(client.deliveries.request(changeset["id"])["id"])
    print("pull request:", (delivery.get("pull_request") or {}).get("url"))


if __name__ == "__main__":
    main(sys.argv[1])

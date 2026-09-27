"""Verify SBX_BASE_URL + SBX_API_KEY against /v1/me."""

from __future__ import annotations

import json

from sbx.sdk import SbxClient


def run(client: SbxClient) -> dict:
    return client.me()


def main() -> None:
    with SbxClient() as client:
        print(json.dumps(run(client), indent=2))


if __name__ == "__main__":
    main()

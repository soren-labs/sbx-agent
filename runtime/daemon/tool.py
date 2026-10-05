"""Narrow shell transport for official CLIs; no model loop or provider credential API."""

import argparse
import json
import os
from urllib.parse import urlparse

import httpx


def main():
    parser = argparse.ArgumentParser(description="Session-scoped SBX application tool")
    parser.add_argument(
        "name",
        choices=["spawn", "message", "wait", "result", "cancel", "publish_result", "read", "apply"],
    )
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--arguments", default="{}")
    args = parser.parse_args()
    url, grant = os.environ.get("SBX_TOOL_URL"), os.environ.get("SBX_TOOL_GRANT")
    if not url or not grant or urlparse(url).scheme != "https":
        raise SystemExit("Tool gateway unavailable")
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.post(
            url,
            json={"name": args.name, "arguments": json.loads(args.arguments)},
            headers={"Authorization": "Bearer " + grant, "Idempotency-Key": args.operation_id},
        )
    if response.status_code >= 400:
        raise SystemExit("Tool command rejected: " + str(response.status_code))
    print(json.dumps(response.json()))


if __name__ == "__main__":
    main()

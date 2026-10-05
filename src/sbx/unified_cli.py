"""Resource CLI. Credential bodies are protected stdin, never argv values."""

import argparse
import json
import sys

from sbx.sdk.unified import Client


def main():
    parser = argparse.ArgumentParser(prog="sbx")
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument(
        "resource",
        choices=[
            "projects",
            "connections",
            "sessions",
            "turns",
            "changesets",
            "deliveries",
            "delegations",
            "operations",
        ],
    )
    parser.add_argument("action")
    parser.add_argument("id", nargs="?")
    parser.add_argument("--workspace")
    args = parser.parse_args()
    client = Client(args.url)
    # Login and operation data travel only through stdin in one bounded process.
    document = json.load(sys.stdin)
    if "login" in document:
        client.login(document["login"]["email"], document["login"]["password"])
    body = document.get("body", {})
    aliases = {
        "send": "messages",
        "cancel": "cancellations",
        "capture": "changesets",
        "apply": "applications",
        "request": "deliveries",
        "retry": "retries",
        "merge": "merge-requests",
        "spawn": "delegations",
        "wait": "waits",
        "result": "result",
        "disconnect": "DELETE",
        "replace": "credential-versions",
        "validate": "validations",
        "close": "closures",
    }
    if args.action == "create":
        result = client.create(args.workspace, args.resource, body)
    elif args.action == "list":
        result = client.request("GET", f"/api/workspaces/{args.workspace}/{args.resource}")
    elif args.action in {"get", "show"}:
        result = getattr(client, args.resource).get(args.id)
    elif args.action == "disconnect":
        result = client.request("DELETE", f"/api/connections/{args.id}", body)
    else:
        result = getattr(client, args.resource).action(
            args.id, aliases.get(args.action, args.action), body
        )
    # API keys are intentionally omitted from CLI logs; an interactive issuance UX is later.
    if isinstance(result, dict):
        result.pop("key", None)
    print(json.dumps(result, default=str))
    client.close()


if __name__ == "__main__":
    main()

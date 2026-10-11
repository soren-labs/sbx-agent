"""Add a subscription Machine Slot, finish its official sign-in, and optionally run a Session on it.

SBX_BASE_URL=http://127.0.0.1:8800 SBX_API_KEY=... python examples/subscription_machines.py grok
SBX_BASE_URL=... SBX_API_KEY=... python examples/subscription_machines.py antigravity \
    --prompt "Summarize this repository."

You sign in to the provider yourself, in your own browser. For Antigravity the browser shows
a code after sign-in; paste it when asked. It is read without echo and sent once. This script
never reads a provider password or an API key, and no secret is taken from argv.
"""

from __future__ import annotations

import argparse
import os
from getpass import getpass
from typing import Any

from sbx import SBXClient, SBXError

PROVIDERS = ("codex", "grok", "antigravity")


def show_device_code(login: dict[str, Any]) -> None:
    """Device-code providers (Codex, Grok Build): open the URL and enter the one-time code."""
    print(f"Open {login['verification_url']} and enter the code {login['user_code']}.")


def ask_pasted_code(login: dict[str, Any]) -> str | None:
    """Antigravity: open the URL, sign in, then paste the code the browser shows."""
    print(f"Open {login['verification_url']}, sign in, then copy the code the browser shows.")
    return getpass("Paste the code (input is hidden): ").strip() or None


def describe_models(catalog: dict[str, Any]) -> None:
    """Print the models and efforts exactly as the signed-in CLI reported them."""
    if catalog.get("status") != "ready":
        print("models: none reported; a Session uses the provider default")
        return
    print(f"models (source: {catalog.get('source')}):")
    for model in catalog.get("models", []):
        efforts = [e["id"] for e in (model.get("reasoning") or {}).get("efforts", [])]
        print(f"  {model['id']}  efforts: {', '.join(efforts) or 'none'}")


def first_model(catalog: dict[str, Any]) -> dict[str, Any] | None:
    models = catalog.get("models") or []
    defaults = [m for m in models if m.get("default")]
    return (defaults or models or [None])[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Add a subscription machine and sign it in.")
    parser.add_argument("provider", choices=PROVIDERS)
    parser.add_argument("--label", help="a name for the machine (default: the provider name)")
    parser.add_argument("--prompt", help="start one Session on the machine with this prompt")
    parser.add_argument("--timeout", type=float, default=1200.0, help="seconds to wait for sign-in")
    args = parser.parse_args()

    client = SBXClient(
        os.environ.get("SBX_BASE_URL", "http://127.0.0.1:8800"), os.environ["SBX_API_KEY"]
    )
    print("providers:")
    for item in client.slots.providers():
        print(f"  {item['provider_id']}: {item['display_name']} ({item['login_method']})")

    slot = client.slots.add(args.provider, label=args.label or f"{args.provider} machine")
    print("machine:", slot["id"])
    try:
        slot = client.slots.wait(
            slot["id"], deadline=args.timeout, on_code=show_device_code, on_input=ask_pasted_code
        )
    except SBXError as exc:
        if exc.code != "timeout":
            raise
        raise SystemExit(f"sign-in is still pending: sbx slots show {slot['id']}") from None
    if slot["status"] not in ("ready", "running"):
        raise SystemExit(f"machine is {slot['status']}: sbx slots show {slot['id']}")
    print("machine is ready:", slot["id"])

    catalog = client.slots.models(slot["id"])
    describe_models(catalog)

    if args.prompt:
        harness: dict[str, Any] = {"provider_id": args.provider}
        model = first_model(catalog)
        if model is not None:
            harness["model"] = model["id"]
            effort = (model.get("reasoning") or {}).get("default")
            if effort:
                harness["effort"] = effort
        result = client.sessions.create(
            harness=harness,
            executor={"backend": "modal"},
            inference={"mode": "subscription", "machine_slot_id": slot["id"]},
            message={"content": args.prompt},
        )
        print("session:", result["session_id"])


if __name__ == "__main__":
    main()

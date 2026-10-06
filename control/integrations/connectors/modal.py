"""Modal token-pair connector: shape check + authenticated control-plane lookup."""

from __future__ import annotations

from typing import Any

from control.integrations.connectors.base import Observation, require

KIND = "modal"
FORMAT = "modal_token_pair/v1"


def normalize(credential: dict[str, Any]) -> dict[str, Any]:
    require(credential, "token_id", "token_secret")
    token_id, secret = credential["token_id"].strip(), credential["token_secret"].strip()
    return {"token_id": token_id, "token_secret": secret}


def validate(credential: dict[str, Any], *, sdk: Any = None) -> Observation:
    if sdk is None:
        import modal as sdk  # noqa: PLC0415 - only for real validation
    try:
        client = sdk.Client.from_credentials(credential["token_id"], credential["token_secret"])
        app = sdk.App.lookup("sbx-executor", create_if_missing=True, client=client)
    except Exception as exc:
        name = type(exc).__name__
        if "Auth" in name or "Permission" in name or "Unauth" in name:
            return Observation("invalid", details={"reason": "modal_rejected_token"})
        return Observation("error", details={"reason": f"modal_unreachable:{name}"})
    return Observation(
        "ready",
        details={"app": "sbx-executor", "app_ready": bool(app.app_id), "probe": "App.lookup"},
    )

"""OpenCode Zen connector: API-key validation via the official Zen model
catalog (a free metadata probe — not a billable turn) + usable-model
discovery preferring free models (RFC 167 §06: capability discovery must
avoid billable Turns when an official metadata command suffices)."""

from __future__ import annotations

import httpx

from control.connectors.base import ConnectorResult

_MODELS_URL = "https://opencode.ai/zen/v1/models"

# Free Zen models carry a "-free" suffix; "big-pickle" is free without it.
_ALWAYS_FREE_IDS = frozenset({"big-pickle"})
_FREE_SUFFIX = "-free"


def model_entry_to_dict(entry: dict) -> dict:
    """Normalize one catalog row -> {id (opencode/<id>), free, usable}."""
    raw_id = entry.get("id") or ""
    free = raw_id in _ALWAYS_FREE_IDS or raw_id.endswith(_FREE_SUFFIX)
    cost = entry.get("cost")
    if cost is not None:
        vals = [v for v in cost.values() if isinstance(v, (int, float)) and v not in (0,)]
        free = free or not vals
    return {
        "id": f"opencode/{raw_id}",
        "raw_id": raw_id,
        "free": free,
        "usable": True,
    }


class OpencodeZenConnector:
    kind = "opencode_zen"

    def discover_models(self, payload: dict) -> list[dict] | None:
        """Full usable catalog; auth errors surface as validation failure."""
        key = payload.get("api_key") or ""
        resp = httpx.get(
            _MODELS_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=20,
        )
        if resp.status_code in (401, 403):
            return None
        resp.raise_for_status()
        entries = (resp.json() or {}).get("data") or []
        return [model_entry_to_dict(e) for e in entries if e.get("id")]

    def validate(self, fmt: str, payload: dict) -> ConnectorResult:
        if fmt != "api_key":
            return ConnectorResult(
                ok=False, reason="unsupported_format", message="expected api_key"
            )
        try:
            models = self.discover_models(payload)
        except httpx.HTTPError as exc:
            return ConnectorResult(
                ok=False,
                reason="probe_failed",
                message=f"Zen catalog unreachable ({type(exc).__name__})",
            )
        if models is None:
            return ConnectorResult(
                ok=False, reason="auth_failed", message="Zen rejected the API key"
            )
        usable = [m["id"] for m in models if m["usable"]]
        free = [m["id"] for m in models if m["usable"] and m["free"]]
        return ConnectorResult(
            ok=True,
            capabilities={
                "runtime_execution": {
                    "models": usable,
                    "free_models": free,
                    "preferred_model": free[0] if free else (usable[0] if usable else None),
                }
            },
        )

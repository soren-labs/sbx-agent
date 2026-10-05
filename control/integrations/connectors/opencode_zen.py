"""OpenCode Zen API key connector.

Validation sends one minimal chat request to a free model: HTTP 401 means the
key is rejected; the free-tier gate response ("only from within OpenCode")
means the key authenticated. The call can count against provider quota and is
recorded as quota-consuming. The model catalog comes from Zen's model list plus
public models.dev pricing; free models are marked usable via the official
OpenCode CLI, which is the only Harness that runs them.
"""

from __future__ import annotations

from typing import Any

import httpx

from control.integrations.connectors.base import Observation, require

KIND = "opencode_zen"
FORMAT = "zen_api_key/v1"
BASE = "https://opencode.ai/zen/v1"
PRICING = "https://models.dev/api.json"
PREFERRED_FREE = (
    "big-pickle",
    "mimo-v2.5-free",
    "nemotron-3.5-lightning-free",
    "ling-3.1-flash-free",
)
HEADERS = {"User-Agent": "sbx-browser/0.2 (+official opencode harness)"}


def normalize(credential: dict[str, Any]) -> dict[str, Any]:
    require(credential, "api_key", min_len=10)
    return {"api_key": credential["api_key"].strip()}


def _catalog(http: Any) -> dict[str, Any]:
    models = http.get(f"{BASE}/models", headers=HEADERS, timeout=20).json().get("data", [])
    try:
        pricing = http.get(PRICING, timeout=30).json().get("opencode", {}).get("models", {})
    except Exception:
        pricing = {}
    items = []
    for model in models:
        model_id = model.get("id")
        cost = (pricing.get(model_id) or {}).get("cost") or {}
        free = model_id.endswith("-free") or (cost.get("input") == 0 and cost.get("output") == 0)
        items.append(
            {
                "id": f"opencode/{model_id}",
                "provider_model": model_id,
                "free": bool(free),
                "usable_via": "official_opencode_cli",
                "pricing_known": bool(cost),
            }
        )
    preferred = next(
        (
            f"opencode/{m}"
            for m in PREFERRED_FREE
            if any(i["provider_model"] == m and i["free"] for i in items)
        ),
        None,
    )
    if preferred is None:
        preferred = next((i["id"] for i in items if i["free"]), items[0]["id"] if items else None)
    items.sort(key=lambda i: (not i["free"], i["id"] != preferred, i["id"]))
    return {
        "models": items,
        "preferred_model": preferred,
        "source": "zen:/v1/models + models.dev pricing",
    }


def validate(credential: dict[str, Any], *, client: Any = None) -> Observation:
    http = client or httpx
    try:
        response = http.post(
            f"{BASE}/chat/completions",
            json={
                "model": PREFERRED_FREE[0],
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
            headers={**HEADERS, "Authorization": f"Bearer {credential['api_key']}"},
            timeout=60,
        )
    except httpx.HTTPError as exc:
        return Observation("error", details={"reason": f"zen_unreachable:{type(exc).__name__}"})
    kind = ""
    try:
        kind = str((response.json().get("error") or {}).get("type") or "")
    except ValueError:
        pass
    if response.status_code == 401 or kind == "AuthError":
        return Observation("invalid", details={"reason": "zen_rejected_key"}, quota_consuming=True)
    if response.status_code == 429:
        return Observation(
            "degraded",
            details={"reason": "zen_rate_limited"},
            quota_consuming=True,
            retry_after=float(response.headers.get("retry-after") or 60),
        )
    if response.status_code not in (200, 403) or (
        response.status_code == 403 and kind != "FreeTierError"
    ):
        return Observation(
            "error",
            details={"reason": "zen_unexpected", "status": response.status_code},
            quota_consuming=True,
        )
    try:
        catalog = _catalog(http)
    except Exception as exc:
        return Observation(
            "degraded",
            details={"reason": f"catalog_unavailable:{type(exc).__name__}", "auth": "accepted"},
            quota_consuming=True,
        )
    return Observation(
        "ready",
        details={"auth": "accepted", "probe": "chat.completions minimal free-model request"},
        catalog=catalog,
        quota_consuming=True,
    )

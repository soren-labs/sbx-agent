"""BYOK inference input for the opt-in live checks (never printed).

``SBX_TEST_INFERENCE_API_KEY`` is required. Endpoints and model default to DeepSeek and
can be overridden with ``SBX_TEST_INFERENCE_ENDPOINTS`` (JSON ``{protocol: base_url}``)
and ``SBX_TEST_INFERENCE_MODEL``.
"""

from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_ENDPOINTS = {
    "openai_chat": "https://api.deepseek.com",
    "openai_responses": "https://api.deepseek.com",
    "anthropic_messages": "https://api.deepseek.com/anthropic",
}
ENV = ("SBX_TEST_INFERENCE_API_KEY", "SBX_TEST_INFERENCE_ENDPOINTS", "SBX_TEST_INFERENCE_MODEL")


def inference_credential() -> dict[str, Any]:
    endpoints = os.environ.get("SBX_TEST_INFERENCE_ENDPOINTS")
    return {
        "api_key": os.environ["SBX_TEST_INFERENCE_API_KEY"],
        "model": os.environ.get("SBX_TEST_INFERENCE_MODEL", "deepseek-flash"),
        "endpoints": json.loads(endpoints) if endpoints else DEFAULT_ENDPOINTS,
    }

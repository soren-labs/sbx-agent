"""Exit codes, usage keys, and timing constants for the Codex runner."""

from __future__ import annotations

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_CODEX = 2
EXIT_TIMEOUT = 3
EXIT_BAD_JSON = 4

DEFAULT_MAX_SECONDS = 900
TERM_GRACE_S = 30
DEFAULT_WORK = "/work"
DEFAULT_PROVIDER_ID = "sbx"

# Five usage fields from Codex 0.153.0 ``turn.completed.usage`` (P0).
USAGE_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

STATUS_SUCCESS = "success"
STATUS_CODEX_ERROR = "codex_error"
STATUS_TIMEOUT = "timeout"
STATUS_BAD_JSON = "bad_json"

SHELL_ENV_EXCLUDE = ["CODEX_AUTH_JSON", "SBX_PROVIDER_API_KEY"]

SANDBOX_AGENTS_MD = """# Sandbox AGENTS.md

- Working directory is `/work`. Stay inside it.
- Do not ask for confirmation; continue without waiting for the user.
- For large changes, first describe the plan, then execute.
"""

PLACEHOLDER_AUTH_JSON = {
    "auth_mode": "chatgpt",
    "tokens": {
        "access_token": "REDACTED",
        "refresh_token": "REDACTED",
        "id_token": "REDACTED",
    },
}

PLACEHOLDER_PROVIDER_AUTH_JSON = {
    "auth_mode": "provider",
    "tokens": {"access_token": "REDACTED"},
}

"""Regenerate docs/specs/unified/openapi.yaml from the single /api surface."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from control.api.router import create_api

TARGET = Path(__file__).resolve().parents[1] / "docs" / "specs" / "unified" / "openapi.yaml"


def render() -> str:
    spec = create_api(None).openapi()
    spec["info"]["description"] = (
        "Unified SBX business API (RFC 08). Generated from control/api; CI checks drift. "
        "Errors use {error:{code,category,message,retryable,details,request_id}}. "
        "Mutations require Idempotency-Key; cookie mutations require X-CSRF-Token."
    )
    return yaml.safe_dump(spec, sort_keys=True, allow_unicode=True)


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0 if TARGET.read_text() == render() else 1)
    TARGET.write_text(render())

"""Unified /api — the single business surface (RFC 167 §08)."""

from control.api.app import create_app

__all__ = ["create_app"]

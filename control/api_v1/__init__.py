"""Public REST API v1 (Cursor Cloud Agents shape, ``docs/contracts/api-v1.yaml``).

WP0 ships an empty router; P2-D (SOR-64) implements the endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/v1")

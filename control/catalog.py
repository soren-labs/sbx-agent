"""Harness catalog loaded from published manifest data (never adapter code)."""

from __future__ import annotations

import json
from pathlib import Path

from control.application.resolution import StaticCatalog

MANIFESTS = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "specs"
    / "unified"
    / "harnesses"
    / "manifests.json"
)


def load_catalog(path: Path = MANIFESTS) -> StaticCatalog:
    return StaticCatalog(json.loads(path.read_text()))

"""tests/unit/sbx shared fixtures.

SOR-266: ``sbx deploy`` builds ``console/`` via npm by default and stamps the
bundle with the checkout's git SHA. Unit tests must never shell out to node
or depend on the host checkout state, so every test in this directory runs
against the prebuilt-dist lane (``SBX_CONSOLE_DIST``) and a deterministic
``SBX_GIT_SHA`` matching the fixture manifest/index meta.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sbx_fakes import FAKE_GIT_SHA, make_console_dist


@pytest.fixture(autouse=True)
def _prebuilt_console_dist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SBX_CONSOLE_DIST", str(make_console_dist(tmp_path)))
    monkeypatch.setenv("SBX_GIT_SHA", FAKE_GIT_SHA)

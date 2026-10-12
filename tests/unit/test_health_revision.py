"""/healthz reports the build revision so a deployment can be matched to its commit."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from control.api.router import create_api
from fastapi.testclient import TestClient

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    ("value", "expected"), [(SHA, SHA), (None, None), ("", None), ("unknown", None)]
)
def test_healthz_reports_build_revision(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("SBX_GIT_SHA", raising=False)
    else:
        monkeypatch.setenv("SBX_GIT_SHA", value)
    client = TestClient(create_api(SimpleNamespace()))
    assert client.get("/healthz").json() == {"ok": True, "git_sha": expected}

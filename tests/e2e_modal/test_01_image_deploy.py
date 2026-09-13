"""Deployed control plane is reachable with Basic Auth (no 500)."""

from __future__ import annotations

import httpx

from tests.e2e_modal.helpers import basic_auth, write_json


def test_unauthenticated_sessions_is_401(control_url: str) -> None:
    resp = httpx.get(f"{control_url}/api/sessions", timeout=30.0, follow_redirects=True)
    assert resp.status_code == 401
    assert resp.status_code != 500


def test_authenticated_sessions_is_200(client: httpx.Client, control_url: str) -> None:
    resp = client.get("/api/sessions")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)
    user, _pw = basic_auth()
    write_json(
        "image_deploy.json",
        {"control_url": control_url, "auth_user_len": len(user), "sessions": len(resp.json())},
    )

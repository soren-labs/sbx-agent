"""SOR-214: Provider Connect routes — /v1/auth*.

Sessions run against the real connect service with the pair lane forced
(pure-cloud posture) or the hosted lane driven through the launcher seam;
credential blobs stay in the fake registry — never on any response body.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from control.connect import ProviderConnectService
from control.provider_auth import AUTH_SESSION_STATES

FAKES = Path(__file__).resolve().parents[2] / "fakes"
CODEX_BIN = FAKES / "fake_codex.py"


@pytest.fixture()
def pair_connect(v1_env: Any, tmp_path: Path) -> ProviderConnectService:
    """Cloud posture: no provider CLI on the plane → pair lane."""
    service = ProviderConnectService(
        v1_env.registry,
        env={"SBX_BACKEND": "modal", "HOME": str(tmp_path / "op-home")},
        work_dir=tmp_path / "connect",
        hosted_available=lambda _p: False,
    )
    v1_env.app.state.provider_connect = service
    return service


@pytest.fixture()
def hosted_connect(v1_env: Any, tmp_path: Path) -> ProviderConnectService:
    service = ProviderConnectService(
        v1_env.registry,
        env={
            "SBX_BACKEND": "local",
            "HOME": str(tmp_path / "op-home"),
            "CODEX_BIN": str(CODEX_BIN),
        },
        work_dir=tmp_path / "connect",
        hosted_available=lambda _p: True,
    )
    v1_env.app.state.provider_connect = service
    return service


def _codex_blob() -> dict[str, Any]:
    return {
        "provider": "codex",
        "files": {".codex/auth.json": json.dumps({"token": "REDACTED"})},
    }


class TestIndex:
    def test_auth_index_vocab(self, client: Any, admin_auth: dict, pair_connect: Any) -> None:
        resp = client.get("/v1/auth", headers=admin_auth)
        assert resp.status_code == 200
        body = resp.json()
        assert body["auth_states"] == list(AUTH_SESSION_STATES)
        assert "failed" in body["connect_states"]
        assert "cancelled" in body["connect_states"]
        assert body["sessions"] == []

    def test_requires_admin(self, client: Any, auth: dict, pair_connect: Any) -> None:
        assert client.get("/v1/auth", headers=auth).status_code == 403
        assert client.get("/v1/auth").status_code == 401


class TestPairLane:
    def test_begin_pair_and_complete(
        self, client: Any, admin_auth: dict, pair_connect: Any, v1_env: Any
    ) -> None:
        resp = client.post(
            "/v1/auth/connect", json={"provider": "codex", "label": "mine"}, headers=admin_auth
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["kind"] == "pair"
        assert body["state"] == "authenticating"
        ticket = body["pair_ticket"]
        assert body["pair_command"] == f"sbx auth pair {ticket}"

        # The unauthenticated ticket lookup teaches the CLI the provider.
        info = client.get(f"/v1/auth/pair/{ticket}")
        assert info.status_code == 200
        assert info.json()["provider"] == "codex"

        out = client.post(
            "/v1/auth/pair/complete",
            json={"ticket": ticket, "credential": _codex_blob()},
        )
        assert out.status_code == 200, out.text
        done = out.json()
        assert done["connect"]["state"] in ("verified", "materialized")
        account_id = done["account_id"]
        account = v1_env.registry.get(account_id)
        assert account is not None and account.label == "mine"
        # stub_runner `init` exits 0 → the sandbox probe verifies it.
        assert done["verified"] is True
        assert done["session"]["status"] == "active"
        assert done["session"]["auth_state"] in AUTH_SESSION_STATES
        # Secret-leak: no credential bytes on the wire.
        assert "REDACTED" not in out.text

        # Ticket consumed — replay rejected.
        assert client.get(f"/v1/auth/pair/{ticket}").status_code == 401

        # And the session shows a terminal canonical-derived state.
        sess = client.get(f"/v1/auth/connect/{body['id']}", headers=admin_auth)
        assert sess.json()["state"] == "verified"
        assert "pair_ticket" not in sess.json()

    def test_begin_requires_admin(self, client: Any, auth: dict, pair_connect: Any) -> None:
        assert (
            client.post("/v1/auth/connect", json={"provider": "codex"}, headers=auth).status_code
            == 403
        )
        assert client.post("/v1/auth/connect", json={"provider": "codex"}).status_code == 401

    def test_unknown_provider_400(self, client: Any, admin_auth: dict, pair_connect: Any) -> None:
        resp = client.post("/v1/auth/connect", json={"provider": "bogus"}, headers=admin_auth)
        assert resp.status_code == 400

    def test_pair_bad_ticket_401(self, client: Any, pair_connect: Any) -> None:
        assert client.get("/v1/auth/pair/sbxp_nope").status_code == 401
        resp = client.post(
            "/v1/auth/pair/complete",
            json={"ticket": "sbxp_nope", "credential": _codex_blob()},
        )
        assert resp.status_code == 401

    def test_pair_bad_blob_400_ticket_preserved(
        self, client: Any, admin_auth: dict, pair_connect: Any
    ) -> None:
        body = client.post(
            "/v1/auth/connect", json={"provider": "codex"}, headers=admin_auth
        ).json()
        resp = client.post(
            "/v1/auth/pair/complete",
            json={"ticket": body["pair_ticket"], "credential": {"files": {}}},
        )
        assert resp.status_code == 400
        # Lookup is unaffected — the ticket survives for a corrected retry.
        assert client.get(f"/v1/auth/pair/{body['pair_ticket']}").status_code == 200

    def test_cancel_and_retry(self, client: Any, admin_auth: dict, pair_connect: Any) -> None:
        body = client.post("/v1/auth/connect", json={"provider": "grok"}, headers=admin_auth).json()
        cancel = client.post(f"/v1/auth/connect/{body['id']}/cancel", headers=admin_auth)
        assert cancel.status_code == 200
        assert cancel.json()["state"] == "cancelled"
        retry = client.post(f"/v1/auth/connect/{body['id']}/retry", headers=admin_auth)
        assert retry.status_code == 201
        assert retry.json()["id"] != body["id"]
        assert retry.json()["pair_ticket"].startswith("sbxp_")

    def test_get_missing_session_404(
        self, client: Any, admin_auth: dict, pair_connect: Any
    ) -> None:
        assert client.get("/v1/auth/connect/conn-nope", headers=admin_auth).status_code == 404

    def test_relink_round_trip(
        self, client: Any, admin_auth: dict, pair_connect: Any, v1_env: Any
    ) -> None:
        resp = client.post(
            "/v1/auth/connect",
            json={"provider": "codex", "account_id": "acct-codex-1"},
            headers=admin_auth,
        )
        body = resp.json()
        assert body["relink"] is True
        out = client.post(
            "/v1/auth/pair/complete",
            json={"ticket": body["pair_ticket"], "credential": _codex_blob()},
        )
        assert out.status_code == 200
        assert out.json()["account_id"] == "acct-codex-1"

    def test_relink_missing_account_404(
        self, client: Any, admin_auth: dict, pair_connect: Any
    ) -> None:
        resp = client.post(
            "/v1/auth/connect",
            json={"provider": "codex", "account_id": "acct-missing"},
            headers=admin_auth,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "account_not_found"


class TestHostedLane:
    def test_hosted_session_surfaces_url_and_verifies(
        self, client: Any, admin_auth: dict, hosted_connect: Any, tmp_path: Path
    ) -> None:
        """The hosted lane spawns the real fake-codex login under a scratch
        HOME — output scraping surfaces the device URL/code live."""
        resp = client.post("/v1/auth/connect", json={"provider": "codex"}, headers=admin_auth)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["kind"] == "hosted"
        assert "pair_ticket" not in body

        deadline = time.monotonic() + 15
        seen: dict[str, Any] = {}
        while time.monotonic() < deadline:
            seen = client.get(f"/v1/auth/connect/{body['id']}", headers=admin_auth).json()
            if seen["state"] in ("verified", "materialized", "failed"):
                break
            time.sleep(0.1)
        assert seen["state"] == "verified", seen.get("error")
        assert seen["browser_url"] == "https://sbx.invalid/device"
        assert seen["user_code"] == "FAKE-1234"
        assert seen["account_id"]
        # Operator HOME untouched — the credential lived in the scratch dir.
        assert not (tmp_path / "op-home" / ".codex").exists()

    def test_hosted_cancel_stops_login(
        self, client: Any, admin_auth: dict, v1_env: Any, tmp_path: Path
    ) -> None:
        service = ProviderConnectService(
            v1_env.registry,
            env={"SBX_BACKEND": "local", "HOME": str(tmp_path / "op-home")},
            work_dir=tmp_path / "connect",
            hosted_available=lambda _p: True,
            launcher=lambda argv, env, on_output: time.sleep(30) and 0,
        )
        v1_env.app.state.provider_connect = service
        body = client.post(
            "/v1/auth/connect", json={"provider": "codex"}, headers=admin_auth
        ).json()
        out = client.post(f"/v1/auth/connect/{body['id']}/cancel", headers=admin_auth).json()
        assert out["state"] == "cancelled"

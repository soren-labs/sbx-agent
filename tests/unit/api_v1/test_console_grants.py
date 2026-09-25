"""SOR-211: one-time Console handoff tickets — mint (admin) → exchange once.

The grant is the safe browser-admin handoff behind ``sbx open``: the
long-lived ``sbx_`` key never enters a URL; a single-use, short-TTL ticket
does — riding the URL fragment, which never reaches the server.
"""

from __future__ import annotations

from typing import Any

from control.api_v1.state import ConsoleGrantStore


def _grant(client: Any, admin_auth: dict[str, str]) -> dict[str, Any]:
    resp = client.post("/v1/console/grant", headers=admin_auth)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_grant_requires_admin_scope(client, auth) -> None:
    """An agents-scoped key cannot mint handoff tickets."""
    resp = client.post("/v1/console/grant", headers=auth)
    assert resp.status_code == 403


def test_grant_and_exchange_mint_a_working_key(client, admin_auth) -> None:
    minted = _grant(client, admin_auth)
    assert minted["grant"].startswith("sbxg_")
    assert minted["expires_in"] > 0

    resp = client.post("/v1/console/exchange", json={"grant": minted["grant"]})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    key = body["key"]
    assert key.startswith("sbx_")
    assert set(body["scopes"]) == {"agents", "admin"}

    # The minted key authenticates immediately — including admin routes.
    me = client.get("/v1/me", headers={"Authorization": f"Bearer {key}"})
    assert me.status_code == 200
    keys = client.get("/v1/api-keys", headers={"Authorization": f"Bearer {key}"})
    assert keys.status_code == 200


def test_exchange_is_single_use(client, admin_auth) -> None:
    grant = _grant(client, admin_auth)["grant"]
    first = client.post("/v1/console/exchange", json={"grant": grant})
    assert first.status_code == 201
    replay = client.post("/v1/console/exchange", json={"grant": grant})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "grant_invalid"


def test_exchange_rejects_unknown_ticket(client) -> None:
    resp = client.post("/v1/console/exchange", json={"grant": "sbxg_bogus"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "grant_invalid"


def test_exchange_validates_body(client) -> None:
    """Malformed bodies are the canonical 400 validation error."""
    assert client.post("/v1/console/exchange", json={}).status_code == 400
    assert client.post("/v1/console/exchange", json={"grant": ""}).status_code == 400


class TestGrantStore:
    """Store semantics independent of HTTP: hashed, single-use, expiring."""

    def test_stores_hashes_not_tickets(self) -> None:
        store = ConsoleGrantStore()
        ticket, _ = store.create()
        assert all(ticket not in digest for digest in store._grants)
        assert store.consume(ticket)

    def test_consume_expired(self) -> None:
        store = ConsoleGrantStore()
        ticket, expires_at = store.create(ttl_s=10)
        assert not store.consume(ticket, now=expires_at + 1)

    def test_create_prunes_expired(self) -> None:
        store = ConsoleGrantStore()
        ticket, _ = store.create(ttl_s=1)
        import time

        time.sleep(1.05)
        store.create()  # prune sweeps the expired ticket
        assert len(store._grants) == 1

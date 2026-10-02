"""Incremental history is authorized and stable when new turns append."""

from tests.unit.api_v2.conftest import create_session, wait_session


def test_history_pages_do_not_shift(client, auth, admin_auth, v1_env):
    session = create_session(client, auth)["session"]
    sid = session["id"]
    wait_session(client, auth, sid, "finished")
    for prompt in ("Second turn", "Third turn"):
        response = client.post(
            f"/v2/sessions/{sid}/messages", json={"prompt": prompt}, headers=auth
        )
        assert response.status_code == 202
        wait_session(client, auth, sid, "finished")
    first = client.get(f"/v2/sessions/{sid}/history?limit=2", headers=auth)
    assert first.status_code == 200, first.text
    page = first.json()
    assert [r["n"] for r in page["runs"]] == [2, 3]
    assert page["has_more"] and page["next_before_n"] == 2
    older = client.get(f"/v2/sessions/{sid}/history?before_n=2&limit=2", headers=auth).json()
    assert [r["n"] for r in older["runs"]] == [1]
    assert older["has_more"] is False
    assert all(e["event"].get("n") == 1 for e in older["events"])
    other_key, other_token = v1_env.keys.create(label="other", scopes=("agents",))
    denied = client.get(
        f"/v2/sessions/{sid}/history", headers={"Authorization": "Bearer " + other_token}
    )
    assert denied.status_code == 404
    assert client.get(f"/v2/sessions/{sid}/history?limit=100", headers=auth).status_code == 400

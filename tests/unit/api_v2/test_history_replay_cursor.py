import httpx
from tests.unit.api_v2.conftest import create_session, wait_session
from tests.unit.api_v2.test_events import _read_events


def test_after_n_replays_only_newer_turns_and_keeps_status(client, auth, live_base):
    sid = create_session(client, auth)["session"]["id"]
    wait_session(client, auth, sid, "finished")
    for prompt in ("Second", "Third"):
        assert (
            client.post(
                f"/v2/sessions/{sid}/messages", headers=auth, json={"prompt": prompt}
            ).status_code
            == 202
        )
        wait_session(client, auth, sid, "finished")
    with httpx.Client(base_url=live_base, timeout=10.0) as http:
        frames, keepalive = _read_events(http, f"/v2/sessions/{sid}/events?after_n=2", auth)
    assert keepalive
    assert frames[0][1] == "session.status"
    assert frames[0][0] is None
    assert any(frame[2].get("n") == 3 for frame in frames)
    assert all(not frame[2].get("n") or frame[2]["n"] > 2 for frame in frames)
    ids = [frame[0] for frame in frames if frame[0] is not None]
    assert ids == sorted(ids)
    assert client.get(f"/v2/sessions/{sid}/events?after_n=-1", headers=auth).status_code == 400

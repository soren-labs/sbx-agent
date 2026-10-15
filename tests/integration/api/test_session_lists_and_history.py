"""Session lists beyond one page, lifecycle filters, search, event history and lifecycle
action eligibility (issue #228 AUD12/AUD13/AUD14/AUD22). Local fixtures only: no Turn
here ever starts compute."""

from __future__ import annotations

import pytest
from tests.support.api import ApiStack, User, inference

SPEC = {"defaults": {"harness": {"provider_id": "opencode"}, "executor": {"backend": "local"}}}


@pytest.fixture
def env(db, tmp_path):
    stack = ApiStack(db, tmp_path)
    user = User(stack)
    user.connect("inference_api", inference("zen-key-lists-000000"))
    stack.drain()
    project = user.post(
        f"/api/workspaces/{user.workspace_id}/projects", {"slug": "lists", "spec": SPEC}
    ).json()
    yield stack, user, project
    stack.shutdown()


def create(user: User, project: dict, title: str, message: str | None = None) -> dict:
    body: dict = {"project_id": project["id"], "title": title}
    if message:
        body["message"] = {"content": message}
    r = user.post(f"/api/workspaces/{user.workspace_id}/sessions", body)
    assert r.status_code in (200, 201, 202), r.text
    return r.json()


def page(user: User, **params: str) -> dict:
    r = user.get(f"/api/workspaces/{user.workspace_id}/sessions", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def all_pages(user: User, **params: str) -> list[dict]:
    items: list[dict] = []
    cursor = params.pop("cursor", None)
    while True:
        body = page(user, **params, **({"cursor": cursor} if cursor else {}))
        items += body["items"]
        cursor = body["next_cursor"]
        if not cursor:
            return items


def test_sessions_beyond_the_first_page_are_reachable_and_searchable(env) -> None:
    stack, user, project = env
    ids = [create(user, project, f"fixture {i:02d}")["session_id"] for i in range(53)]
    oldest = ids[0]
    archived, closed = ids[10:12], ids[20:22]
    for sid in archived:
        assert user.post(f"/api/sessions/{sid}/archives", {}).status_code in (200, 202)
    for sid in closed:
        assert user.post(f"/api/sessions/{sid}/closures", {}).status_code in (200, 202)

    # Default list (AUD13): open only, 49 sessions over two pages without overlap.
    first = page(user, lifecycle="open", limit="40")
    assert len(first["items"]) == 40 and first["next_cursor"]
    opened = all_pages(user, lifecycle="open", limit="40")
    assert len(opened) == 49 and len({s["id"] for s in opened}) == 49
    assert {s["lifecycle"] for s in opened} == {"open"}
    assert oldest in {s["id"] for s in opened}, "the 53rd-newest session is reachable"

    both = all_pages(user, lifecycle="open,archived", limit="50")
    assert len(both) == 51 and set(archived) <= {s["id"] for s in both}
    assert {s["id"] for s in all_pages(user, lifecycle="closed")} == set(closed)
    assert len(all_pages(user, limit="50")) == 53  # no filter: every lifecycle
    bad = user.get(f"/api/workspaces/{user.workspace_id}/sessions?lifecycle=open,deleted")
    assert bad.status_code == 422

    # Search (AUD14) covers the whole workspace, not a loaded page.
    found = page(user, lifecycle="open", q="FIXTURE 00")["items"]
    assert [s["id"] for s in found] == [oldest]
    assert [s["id"] for s in page(user, q=oldest)["items"]] == [oldest]
    assert page(user, q="%")["items"] == [], "LIKE wildcards are literal"
    assert page(user, q="fixture 1", lifecycle="archived")["items"][0]["id"] in archived


def test_paging_survives_a_session_moving_to_the_top(env) -> None:
    stack, user, project = env
    ids = [create(user, project, f"move {i:02d}")["session_id"] for i in range(12)]
    first = page(user, lifecycle="open", limit="5")
    # A session not yet listed is touched (newest activity): it moves to the top. Keyset
    # paging never repeats a listed one; the moved one is on page one when it is re-read.
    moved = ids[0]
    version = user.get(f"/api/sessions/{moved}").json()["session"]["version"]
    renamed = user.patch(
        f"/api/sessions/{moved}", {"title": "moved to top", "expected_version": version}
    )
    assert renamed.status_code == 200, renamed.text
    rest = all_pages(user, lifecycle="open", limit="5", cursor=first["next_cursor"])
    listed = [s["id"] for s in first["items"]] + [s["id"] for s in rest]
    assert len(listed) == len(set(listed)), "no session is listed twice"
    assert page(user, lifecycle="open", limit="5")["items"][0]["id"] == moved


def test_event_history_pages_backwards_and_matches_the_forward_journal(env) -> None:
    stack, user, project = env
    sid = create(user, project, "history")["session_id"]
    for _ in range(4):
        user.post(f"/api/sessions/{sid}/archives", {})
        user.post(f"/api/sessions/{sid}/unarchives", {})
    forward = user.get(f"/api/sessions/{sid}/events?after=0&limit=1000").json()
    watermark = forward["event_watermark"]
    assert len(forward["items"]) == watermark >= 9

    pages, before = [], watermark + 1
    while before:
        body = user.get(f"/api/sessions/{sid}/events?before={before}&limit=3").json()
        assert len(body["items"]) <= 3
        assert [e["seq"] for e in body["items"]] == sorted(e["seq"] for e in body["items"])
        pages.append(body["items"])
        before = body["next_before"]
    history = [e["seq"] for p in reversed(pages) for e in p]
    assert history == [e["seq"] for e in forward["items"]]
    latest = user.get(f"/api/sessions/{sid}/events?before={watermark + 1}&limit=2").json()
    assert [e["seq"] for e in latest["items"]] == [watermark - 1, watermark]
    assert user.get(f"/api/sessions/{sid}/events?before=5&after=1").status_code == 400
    other = User(stack)
    assert other.get(f"/api/sessions/{sid}/events?before=5").status_code == 404


def test_turn_actions_follow_the_session_lifecycle(env) -> None:
    stack, user, project = env
    turns = {}
    for name in ("open", "archived", "closed"):
        created = create(user, project, name, message="never runs")
        # Cancelled while still queued: no Job ran, so no compute was ever requested.
        cancel = user.post(f"/api/turns/{created['turn_id']}/cancellations", {})
        assert cancel.status_code in (200, 202), cancel.text
        turns[name] = (created["session_id"], created["turn_id"])
    user.post(f"/api/sessions/{turns['archived'][0]}/archives", {})
    user.post(f"/api/sessions/{turns['closed'][0]}/closures", {})

    for name, (sid, turn_id) in turns.items():
        listed = user.get(f"/api/sessions/{sid}/turns").json()["items"][0]
        single = user.get(f"/api/turns/{turn_id}").json()["turn"]
        assert listed["state"] == "cancelled"
        offered = "retry" in listed["actions"]
        assert offered == (name == "open") and single["actions"] == listed["actions"]
        retried = user.post(f"/api/turns/{turn_id}/retries", {})
        # The server's eligibility and the command agree in every lifecycle.
        if offered:
            assert retried.status_code in (200, 201, 202), retried.text
        else:
            assert retried.status_code == 409
            assert retried.json()["error"]["code"] == "invalid_transition"
    leases = stack.db.read(lambda u: u.count("executor_leases", {}))
    assert leases == 0

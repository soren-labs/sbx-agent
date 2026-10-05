"""``GET /v2/sessions/{id}/changes/diff``: the file-level Changes view.

The endpoint serves the durable ``patch.diff`` of a ready revision — a
compact file list (paths, status, +/- counts) by default and one file's
diff section on demand via ``?path=``, so the console can lazy-load diffs
without embedding them in the session detail.
"""

from __future__ import annotations

import time

import pytest
from control.api_v2.diff import parse_unified_diff
from fastapi.testclient import TestClient
from tests.unit.api_v1.conftest import V1Env
from tests.unit.api_v2.conftest import wait_session
from tests.unit.api_v2.test_changes_deliver import (
    _agent_id,
    _make_session,
    commit_in_agent,
    make_origin,
    needs_git,
    wait_idle,
)


@pytest.fixture
def origin(tmp_path):
    return make_origin(tmp_path)


def _wait_ready_revision(
    client: TestClient, auth: dict[str, str], session_id: str, head: str, timeout: float = 15.0
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/v2/sessions/{session_id}/changes", headers=auth)
        assert resp.status_code == 200, resp.text
        for revision in resp.json()["revisions"]:
            if revision["status"] == "ready" and revision["head_sha"] == head:
                return revision
        time.sleep(0.1)
    raise AssertionError(f"no ready revision at {head} for session {session_id}")


class TestParseUnifiedDiff:
    def test_modified_added_deleted_renamed(self) -> None:
        patch = (
            "diff --git a/m.txt b/m.txt\n"
            "index 111..222 100644\n"
            "--- a/m.txt\n"
            "+++ b/m.txt\n"
            "@@ -1,2 +1,3 @@\n"
            " keep\n"
            "-gone\n"
            "+new1\n"
            "+new2\n"
            "diff --git a/new.txt b/new.txt\n"
            "new file mode 100644\n"
            "index 000..333\n"
            "--- /dev/null\n"
            "+++ b/new.txt\n"
            "@@ -0,0 +1 @@\n"
            "+hello\n"
            "diff --git a/gone.txt b/gone.txt\n"
            "deleted file mode 100644\n"
            "index 444..000\n"
            "--- a/gone.txt\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-x\n"
            "-y\n"
            "diff --git a/old.txt b/renamed.txt\n"
            "similarity index 90%\n"
            "rename from old.txt\n"
            "rename to renamed.txt\n"
            "index 555..666 100644\n"
            "--- a/old.txt\n"
            "+++ b/renamed.txt\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
        )
        files = {f.path: f for f in parse_unified_diff(patch)}
        assert set(files) == {"m.txt", "new.txt", "gone.txt", "renamed.txt"}
        assert files["m.txt"].status == "modified"
        assert (files["m.txt"].additions, files["m.txt"].deletions) == (2, 1)
        assert files["new.txt"].status == "added"
        assert files["gone.txt"].status == "deleted"
        assert (files["gone.txt"].additions, files["gone.txt"].deletions) == (0, 2)
        assert files["renamed.txt"].status == "renamed"
        assert files["renamed.txt"].old_path == "old.txt"
        # Each file's body is its own diff --git section.
        assert files["new.txt"].body.startswith("diff --git a/new.txt")
        assert "+hello" in files["new.txt"].body
        # ---/+++ headers and context lines are never counted.
        assert files["m.txt"].additions == 2

    def test_empty_and_binary(self) -> None:
        assert parse_unified_diff("") == []
        files = parse_unified_diff(
            "diff --git a/logo.png b/logo.png\n"
            "index 111..222 100644\n"
            "Binary files a/logo.png and b/logo.png differ\n"
        )
        assert files[0].path == "logo.png"
        assert files[0].status == "modified"
        assert (files[0].additions, files[0].deletions) == (0, 0)


@needs_git
class TestChangesDiff:
    def _materialize(self, client, auth, credentialed, origin) -> tuple[dict, str]:
        session = _make_session(client, auth, origin)
        agent_id = _agent_id(credentialed, session["id"])
        wait_idle(credentialed, agent_id)
        head = commit_in_agent(credentialed, agent_id, "b.txt", "two\n")
        client.post(
            f"/v2/sessions/{session['id']}/messages",
            json={"prompt": "more work"},
            headers=auth,
        )
        # The session already reads "finished" from the first run, so wait for
        # this run's revision rather than the status.
        _wait_ready_revision(client, auth, session["id"], head)
        wait_session(client, auth, session["id"], "finished")
        return session, head

    def test_diff_no_revision_404(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        session = _make_session(client, auth, origin)
        wait_idle(credentialed, _agent_id(credentialed, session["id"]))
        resp = client.get(f"/v2/sessions/{session['id']}/changes/diff", headers=auth)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "revision_not_found"

    def test_diff_file_list_and_stats(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        session, head = self._materialize(client, auth, credentialed, origin)
        resp = client.get(f"/v2/sessions/{session['id']}/changes/diff", headers=auth)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["n"] == 1
        assert data["head_sha"] == head
        assert data["base_sha"] == origin[1]
        assert data["files_changed"] == 1
        assert data["additions"] == 1
        assert data["deletions"] == 0
        assert len(data["files"]) == 1
        f = data["files"][0]
        assert f["path"] == "b.txt"
        assert f["status"] == "added"
        # The list view stays compact — no diff bodies embedded.
        assert f["diff"] is None
        # Internal ids never cross the wire.
        for key in ("id", "artifact_id", "agent_id", "task_id", "run_id"):
            assert key not in data
            assert key not in f

    def test_diff_single_file_lazy(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        session, _ = self._materialize(client, auth, credentialed, origin)
        resp = client.get(
            f"/v2/sessions/{session['id']}/changes/diff",
            params={"path": "b.txt"},
            headers=auth,
        )
        assert resp.status_code == 200, resp.text
        files = resp.json()["files"]
        assert len(files) == 1
        assert files[0]["path"] == "b.txt"
        assert files[0]["diff"] is not None
        assert "diff --git" in files[0]["diff"]
        assert "+two" in files[0]["diff"]

    def test_diff_unknown_path_404(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        session, _ = self._materialize(client, auth, credentialed, origin)
        resp = client.get(
            f"/v2/sessions/{session['id']}/changes/diff",
            params={"path": "nope.txt"},
            headers=auth,
        )
        assert resp.status_code == 404

    def test_diff_revision_n_addressing(
        self, client: TestClient, auth: dict[str, str], credentialed: V1Env, origin
    ) -> None:
        session, _ = self._materialize(client, auth, credentialed, origin)
        ok = client.get(
            f"/v2/sessions/{session['id']}/changes/diff",
            params={"n": 1},
            headers=auth,
        )
        assert ok.status_code == 200
        assert ok.json()["n"] == 1
        missing = client.get(
            f"/v2/sessions/{session['id']}/changes/diff",
            params={"n": 9},
            headers=auth,
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "revision_not_found"

    def test_diff_owner_isolation(
        self,
        client: TestClient,
        auth: dict[str, str],
        admin_auth: dict[str, str],
        credentialed: V1Env,
        origin,
    ) -> None:
        session, _ = self._materialize(client, auth, credentialed, origin)
        resp = client.get(f"/v2/sessions/{session['id']}/changes/diff", headers=admin_auth)
        assert resp.status_code == 404

from control.domain.events import digest
from scripts.offline_import import import_export


def test_frozen_mirror_conflicts_unknown_ownership_no_success_and_restart(database, principal):
    content = {
        "title": "Legacy",
        "state": "success",
        "messages": [{"content": "Historical transcript"}],
    }
    records = [
        {
            "record_id": "same",
            "owner_ref": "owner",
            "kind": "session",
            "namespace": "tasks",
            "content": content,
        },
        {
            "record_id": "same",
            "owner_ref": "owner",
            "kind": "session",
            "namespace": "v2",
            "content": content,
        },
        {"record_id": "conflict", "owner_ref": "owner", "kind": "session", "content": content},
        {
            "record_id": "conflict",
            "owner_ref": "owner",
            "kind": "session",
            "content": {**content, "state": "unknown"},
        },
        {
            "record_id": "unknown_owner",
            "owner_ref": "unknown",
            "kind": "session",
            "content": content,
        },
    ]
    export = {
        "source_system": "frozen-sanitized",
        "source_version": "1",
        "source_revision": "baseline",
        "records": records,
        "records_digest": digest(records),
    }
    owners = {"owner": {"user_id": principal.user_id, "workspace_id": principal.workspace_ids[0]}}
    first = import_export(database, export, owners)
    assert first["imported"] == 1
    assert first["conflicts"] == ["conflict"] and first["skipped"] == ["unknown_owner"]
    assert "historical_outcome_not_authoritative" in first["losses"]["same"]
    second = import_export(database, export, owners)
    assert second["replayed"] == 1 and second["map"] == first["map"]
    with database.transaction() as repo:
        assert repo.one("SELECT lifecycle FROM sessions")["lifecycle"] == "archived"
        assert repo.one("SELECT count(*) AS n FROM messages")["n"] == 1
        assert repo.one("SELECT count(*) AS n FROM turns")["n"] == 0
        assert repo.one("SELECT count(*) AS n FROM jobs")["n"] == 0
        assert repo.one("SELECT count(*) AS n FROM delegation_results")["n"] == 0

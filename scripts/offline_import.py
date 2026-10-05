"""Offline sanitized export rehearsal, never imported by the deployed application.

Unknown/inconsistent history becomes an archived Session with provenance and
explicit losses. No imported history authorizes native resume, approval or Git.
"""

import argparse
import json
from pathlib import Path

from control.application.sessions import Sessions
from control.domain.errors import require
from control.domain.events import canonical, digest
from control.domain.identity import Principal
from control.persistence.database import Database
from runtime.security.redaction import SENSITIVE, Redactor


def secret_free(value):
    if isinstance(value, dict):
        return all(
            (not SENSITIVE.search(key) or item in (None, "REDACTED")) and secret_free(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(secret_free(item) for item in value)
    return not isinstance(value, str) or Redactor().clean(value) == value


def import_export(database, export, ownership):
    require(export.get("records_digest") == digest(export["records"]), "capture_failed")
    require(secret_free(export), "forbidden")
    source, version = export["source_system"], export["source_version"]
    report = {"imported": 0, "replayed": 0, "conflicts": [], "skipped": [], "map": {}, "losses": {}}
    grouped = {}
    for record in export["records"]:
        grouped.setdefault(record["record_id"], []).append(record)
    for rid, mirrors in sorted(grouped.items()):
        if len({digest(m["content"]) for m in mirrors}) != 1:
            report["conflicts"].append(rid)
            continue
        record = sorted(mirrors, key=lambda item: item.get("namespace", ""))[0]
        if record.get("kind") != "session" or record.get("owner_ref") not in ownership:
            report["skipped"].append(rid)
            continue
        assignment = ownership[record["owner_ref"]]
        principal = Principal(assignment["user_id"], (assignment["workspace_id"],))
        fingerprint = digest(record)
        with database.transaction() as repo:
            repo.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (source + ":" + version + ":" + rid,),
            )
            previous = repo.one(
                "SELECT * FROM imported_references WHERE source_system=%s AND source_r"
                "ecord_id=%s AND source_version=%s",
                (source, rid, version),
            )
            if previous:
                require(previous["source_digest"] == fingerprint, "idempotency_conflict")
                report["map"][rid] = previous["session_id"]
                report["replayed"] += 1
                continue
            require(
                repo.one(
                    "SELECT id FROM workspaces WHERE id=%s AND owner_id=%s",
                    (assignment["workspace_id"], assignment["user_id"]),
                )
                is not None,
                "forbidden",
            )
            content = record["content"]
            created = Sessions(database).create_in(
                repo,
                principal,
                assignment["workspace_id"],
                {"title": content.get("title", "Imported history")[:200]},
            )
            sid = created["session_id"]
            losses = [
                "native_context_unverified",
                "remote_effects_unresolved",
                "historical_outcome_not_authoritative",
            ]
            if not content.get("messages"):
                losses.append("transcript_missing")
            for message in content.get("messages", []):
                row = repo.one("SELECT * FROM sessions WHERE id=%s FOR UPDATE", (sid,))
                Sessions(database).send_in(
                    repo, row, principal, {"content": message["content"], "routing": "note"}
                )
            repo.execute("UPDATE sessions SET lifecycle='archived' WHERE id=%s", (sid,))
            repo.event(
                assignment["workspace_id"],
                sid,
                "history.imported",
                {
                    "source_system": source,
                    "source_record_id": rid,
                    "source_version": version,
                    "source_revision": export["source_revision"],
                    "source_digest": fingerprint,
                    "losses": losses,
                    "dispatch_disabled": True,
                },
            )
            repo.execute(
                "INSERT INTO imported_references(source_system,source_record_id,source"
                "_version,workspace_id,session_id,source_digest,disposition) VALUES(%s"
                ",%s,%s,%s,%s,%s,%s)",
                (
                    source,
                    rid,
                    version,
                    assignment["workspace_id"],
                    sid,
                    fingerprint,
                    {"losses": losses, "archived": True},
                ),
            )
            report["map"][rid] = sid
            report["losses"][rid] = losses
            report["imported"] += 1
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Frozen sanitized export to disposable unified cohort"
    )
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--export", required=True)
    parser.add_argument("--ownership", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    db = Database(args.dsn)
    db.migrate()
    result = import_export(
        db, json.loads(Path(args.export).read_text()), json.loads(Path(args.ownership).read_text())
    )
    Path(args.report).write_text(canonical(result) + "\n")
    print("Import rehearsal complete; inspect the private disposition report")


if __name__ == "__main__":
    main()

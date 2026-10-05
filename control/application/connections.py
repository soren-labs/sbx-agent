from datetime import UTC, datetime, timedelta

from control.application.access import owned, workspace
from control.application.deduplication import command
from control.domain.connections import PURPOSES, validate_material
from control.domain.errors import require
from control.domain.identity import new_id


class Connections:
    def __init__(self, uow, vault):
        self.uow, self.vault = uow, vault

    @staticmethod
    def context(wid, cid, vid, kind):
        return {
            "workspace_id": wid,
            "connection_id": cid,
            "credential_id": vid,
            "format": kind + "-v1",
        }

    def append(self, repo, connection, material, ordinal):
        cid, wid, kind = connection["id"], connection["workspace_id"], connection["kind"]
        vid = new_id("cred")
        validate_material(kind, material)
        envelope = self.vault.encrypt(material, self.context(wid, cid, vid, kind))
        repo.execute(
            "INSERT INTO credential_versions(id,workspace_id,connection_id,ordinal,for"
            "mat,envelope) "
            "VALUES(%s,%s,%s,%s,%s,%s)",
            (vid, wid, cid, ordinal, kind + "-v1", envelope),
        )
        repo.execute(
            "UPDATE connections SET current_credential_id=%s,health='unverified' WHERE id=%s",
            (vid, cid),
        )
        job = repo.enqueue(wid, "connection.validate", cid, vid)
        repo.execute("UPDATE jobs SET input_credential_id=%s WHERE id=%s", (vid, job))
        return vid

    def create(self, principal, wid, body, key):
        workspace(principal, wid)
        kind = body["kind"]
        with self.uow.transaction() as repo:

            def perform():
                cid = new_id("con")
                repo.execute(
                    "INSERT INTO connections(id,workspace_id,creator_id,kind,label) VA"
                    "LUES(%s,%s,%s,%s,%s)",
                    (cid, wid, principal.user_id, kind, body.get("label", kind)),
                )
                row = repo.one("SELECT * FROM connections WHERE id=%s", (cid,))
                vid = self.append(repo, row, body["credential"], 1)
                return {
                    "connection_id": cid,
                    "credential_version_id": vid,
                    "job_id": repo.one("SELECT id FROM jobs WHERE effect_id=%s", (vid,))["id"],
                    "operation_id": vid,
                    "version": 1,
                    "health": "unverified",
                }

            # Fingerprint secrets in the command receipt without storing the request body.
            return command(repo, principal, wid, "connection.create", key, body, perform)

    def replace(self, principal, cid, body, key):
        with self.uow.transaction() as repo:
            row = owned(repo, "connections", cid, principal, lock=True)

            def perform():
                require(row["version"] == body["expected_version"], "version_conflict")
                require(row["state"] == "configured", "connection_revoked")
                # Reject replacement while live processes may retain old credentials.
                active = repo.one(
                    "SELECT id FROM executor_leases WHERE workspace_id=%s AND cleanup_"
                    "confirmed=false "
                    "AND (connection_id=%s OR session_id IN (SELECT id FROM sessions W"
                    "HERE zen_connection_id=%s))",
                    (row["workspace_id"], cid, cid),
                )
                require(not active, "dependent_resources_active")
                ordinal = repo.one(
                    "SELECT max(ordinal)+1 AS n FROM credential_versions WHERE connection_id=%s",
                    (cid,),
                )["n"]
                vid = self.append(repo, row, body["credential"], ordinal)
                repo.execute(
                    "UPDATE connections SET version=version+1,revocation_epoch=revocat"
                    "ion_epoch+1 WHERE id=%s",
                    (cid,),
                )
                repo.execute(
                    "UPDATE credential_grants SET revoked_at=now() WHERE connection_id"
                    "=%s AND revoked_at IS NULL",
                    (cid,),
                )
                return {
                    "connection_id": cid,
                    "credential_version_id": vid,
                    "job_id": repo.one("SELECT id FROM jobs WHERE effect_id=%s", (vid,))["id"],
                    "operation_id": vid,
                    "version": row["version"] + 1,
                }

            return command(
                repo,
                principal,
                row["workspace_id"],
                "connection.replace",
                key,
                {"connection_id": cid, **body},
                perform,
            )

    def revoke(self, principal, cid, expected_version, key):
        with self.uow.transaction() as repo:
            row = owned(repo, "connections", cid, principal, lock=True)

            def perform():
                require(row["version"] == expected_version, "version_conflict")
                active = repo.one(
                    "SELECT id FROM executor_leases WHERE workspace_id=%s AND cleanup_"
                    "confirmed=false "
                    "AND (connection_id=%s OR session_id IN (SELECT id FROM sessions W"
                    "HERE zen_connection_id=%s))",
                    (row["workspace_id"], cid, cid),
                )
                require(not active, "dependent_resources_active")
                repo.execute(
                    "UPDATE connections SET state='revoked',version=version+1,revocati"
                    "on_epoch=revocation_epoch+1 WHERE id=%s",
                    (cid,),
                )
                repo.execute(
                    "UPDATE credential_grants SET revoked_at=now() WHERE connection_id=%s", (cid,)
                )
                return {"connection_id": cid, "state": "revoked", "version": row["version"] + 1}

            return command(
                repo,
                principal,
                row["workspace_id"],
                "connection.revoke",
                key,
                {"connection_id": cid, "expected_version": expected_version},
                perform,
            )

    def resolve(
        self, principal, cid, purpose, *, session_id=None, lease_id=None, operation_id=None
    ):
        with self.uow.transaction() as repo:
            row = owned(repo, "connections", cid, principal, lock=True)
            require(row["creator_id"] == principal.user_id, "not_found")
            require(row["state"] == "configured", "connection_revoked")
            require(purpose in PURPOSES.get(row["kind"], set()), "forbidden")
            version = owned(repo, "credential_versions", row["current_credential_id"], principal)
            require(
                version["revoked_at"] is None
                and (version["expires_at"] is None or version["expires_at"] > datetime.now(UTC)),
                "credential_invalid",
            )
            if session_id:
                owned(repo, "sessions", session_id, principal)
            if lease_id:
                owned(repo, "executor_leases", lease_id, principal)
            grant = new_id("grant")
            repo.execute(
                "INSERT INTO credential_grants(id,workspace_id,principal_id,connection"
                "_id,credential_id,"
                "purpose,session_id,lease_id,operation_id,epoch,expires_at) VALUES(%s,"
                "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    grant,
                    row["workspace_id"],
                    principal.user_id,
                    cid,
                    version["id"],
                    purpose,
                    session_id,
                    lease_id,
                    operation_id or grant,
                    row["revocation_epoch"],
                    datetime.now(UTC) + timedelta(minutes=10),
                ),
            )
            envelope, context = (
                version["envelope"],
                self.context(row["workspace_id"], cid, version["id"], row["kind"]),
            )
        return self.vault.decrypt(envelope, context), version["id"]

    def safe(self, principal, cid):
        with self.uow.transaction() as repo:
            row = owned(repo, "connections", cid, principal)
            return {
                k: row[k]
                for k in ("id", "workspace_id", "kind", "label", "state", "version", "health")
            }

    def catalog(self, principal, wid):
        workspace(principal, wid)
        with self.uow.transaction() as repo:
            rows = repo.all(
                "SELECT o.capabilities FROM connection_observations o JOIN connections"
                " c ON c.id=o.connection_id "
                "WHERE c.workspace_id=%s AND c.kind='opencode_zen' AND c.state='configured' "
                "AND o.credential_id=c.current_credential_id AND o.status='ready' AND "
                "o.observed_at>now()-interval '1 hour'",
                (wid,),
            )
            models = [m for row in rows for m in row["capabilities"].get("models", [])]
            return sorted(models, key=lambda m: (not m.get("free", False), m["id"]))

    def validate(self, principal, cid, version, key):
        with self.uow.transaction() as repo:
            row = owned(repo, "connections", cid, principal, lock=True)

            def perform():
                require(row["version"] == version, "version_conflict")
                require(row["state"] == "configured", "connection_revoked")
                intent = new_id("validation")
                job = repo.enqueue(row["workspace_id"], "connection.validate", cid, intent)
                repo.execute(
                    "UPDATE jobs SET input_credential_id=%s WHERE id=%s",
                    (row["current_credential_id"], job),
                )
                repo.execute("UPDATE connections SET health='verifying' WHERE id=%s", (cid,))
                return {"connection_id": cid, "job_id": job, "operation_id": intent}

            return command(
                repo,
                principal,
                row["workspace_id"],
                "connection.validate",
                key,
                {"connection_id": cid, "expected_version": version},
                perform,
            )

    def connection_catalog(self, principal, cid):
        with self.uow.transaction() as repo:
            row = owned(repo, "connections", cid, principal)
            observation = (
                repo.one(
                    "SELECT capabilities,observed_at FROM connection_observations "
                    "WHERE connection_id=%s AND credential_id=%s AND status='ready' "
                    "AND observed_at>now()-interval '1 hour' ORDER BY observed_at DESC LIMIT 1",
                    (cid, row["current_credential_id"]),
                )
                if row["state"] == "configured"
                else None
            )
        return {
            "connection_id": cid,
            "observed_at": observation["observed_at"] if observation else None,
            "models": [
                {**m, "connection_id": cid} for m in observation["capabilities"].get("models", [])
            ]
            if observation
            else [],
            "stale": observation is None,
        }

    def read_material(self, principal, cid):
        """Control-owned read transport only; never launch/probe or create a grant."""
        with self.uow.transaction() as repo:
            row = owned(repo, "connections", cid, principal)
            require(
                row["creator_id"] == principal.user_id and row["state"] == "configured",
                "connection_revoked",
            )
            version = repo.one(
                "SELECT * FROM credential_versions WHERE id=%s AND connection_id=%s "
                "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>now())",
                (row["current_credential_id"], cid),
            )
            require(version is not None, "credential_invalid")
        return self.vault.decrypt(
            version["envelope"], self.context(row["workspace_id"], cid, version["id"], row["kind"])
        )

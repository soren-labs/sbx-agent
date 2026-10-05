from control.domain.errors import DomainError
from control.domain.identity import Principal, new_id


class ValidationHandler:
    def __init__(self, uow, claims, connections, connectors):
        self.uow, self.claims, self.connections, self.connectors = (
            uow,
            claims,
            connections,
            connectors,
        )

    def __call__(self, claim):
        cid = claim.row["connection_id"]
        with self.uow.transaction() as repo:
            row = repo.one("SELECT * FROM connections WHERE id=%s", (cid,))
            self.claims.assert_current(repo, claim)
            if (
                row["state"] != "configured"
                or row["current_credential_id"] != claim.row["input_credential_id"]
            ):
                return
            principal = Principal(row["creator_id"], (row["workspace_id"],))
        credential, version = self.connections.resolve(
            principal, cid, "validation", operation_id=claim.row["effect_id"]
        )
        health, observation = "ready", {}
        try:
            observation = self.connectors[row["kind"]].validate(credential)
            for model in observation.get("models", []):
                model["connection_id"] = cid
        except DomainError as error:
            health = "reauth_required" if error.code == "credential_invalid" else "degraded"
            observation = {"reason": error.code}
        with self.uow.transaction() as repo:
            current = repo.one("SELECT * FROM connections WHERE id=%s FOR UPDATE", (cid,))
            self.claims.assert_current(repo, claim)
            if current["state"] != "configured" or current["current_credential_id"] != version:
                return
            repo.execute("UPDATE connections SET health=%s WHERE id=%s", (health, cid))
            repo.execute(
                "INSERT INTO connection_observations(id,workspace_id,connection_id,credential_id,"
                "scope,status,capabilities) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    new_id("observation"),
                    row["workspace_id"],
                    cid,
                    version,
                    observation.get("scope", "validation"),
                    health,
                    observation,
                ),
            )

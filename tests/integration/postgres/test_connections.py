"""Connection + CredentialVersion lifecycle against real Postgres
(RFC 167 §06): create → validation job → replace (CAS + epoch) → grant
redeem → disable/revoke/disconnect, owner isolation, restart durability,
and no plaintext anywhere in persisted rows."""

import json

import pytest
from control.application.connections import (
    PURPOSE_RUNTIME_EXECUTION,
    ConnectionService,
)
from control.application.models import ModelDiscoveryService
from control.connectors.base import ConnectorRegistry, ConnectorResult
from control.domain.errors import DomainError
from control.jobs.handlers import HANDLERS, set_connection_plane
from control.jobs.worker import Worker
from control.persistence.unit_of_work import SqlUnitOfWork
from control.security.vault import Vault

pytestmark = pytest.mark.integration


class FakeZenConnector:
    """Deterministic catalog — no network."""

    kind = "opencode_zen"

    def __init__(self):
        self.calls = 0
        self.fail_auth = False

    def validate(self, fmt, payload):
        self.calls += 1
        if self.fail_auth or payload.get("api_key") == "bad":
            return ConnectorResult(ok=False, reason="auth_failed")
        return ConnectorResult(
            ok=True,
            external_identity={"provider": "zen"},
            capabilities={
                "runtime_execution": {
                    "models": ["opencode/big-pickle", "opencode/paid-1"],
                    "free_models": ["opencode/big-pickle"],
                    "preferred_model": "opencode/big-pickle",
                }
            },
        )

    def discover_models(self, payload):
        if payload.get("api_key") == "bad":
            return None
        return [
            {"id": "opencode/paid-1", "free": False, "usable": True},
            {"id": "opencode/big-pickle", "free": True, "usable": True},
        ]


@pytest.fixture()
def vault():
    return Vault.generate()


@pytest.fixture()
def svc(pg, vault):
    return ConnectionService(pg, vault)


@pytest.fixture()
def zen_connector(pg, vault):
    reg = ConnectorRegistry()
    c = FakeZenConnector()
    reg.register(c)
    set_connection_plane(ConnectionService(pg, vault), reg)
    yield c
    set_connection_plane(None, None)


def _create(svc, pg, ws, kind="opencode_zen", cred=None):
    cred = cred or {"format": "api_key", "payload": {"api_key": "zen-test-key"}}
    with SqlUnitOfWork(pg) as uow:
        return svc.create_connection(
            uow,
            workspace_id=ws["workspace_id"],
            principal_id=ws["user_id"],
            kind=kind,
            label=None,
            credential=cred,
        )


def _drain(pg):
    w = Worker(db=pg, holder="t-worker", handlers=HANDLERS)
    w.run_until_idle()


class TestCreate:
    def test_ciphertext_only_persists(self, pg, svc, workspace):
        conn = _create(svc, pg, workspace)
        assert conn["state"] == "configured"
        assert conn["health"] == "unverified"
        with SqlUnitOfWork(pg) as uow:
            row = uow.credential_versions.get(
                workspace["workspace_id"], conn["current_credential_version_id"]
            )
        assert "zen-test-key" not in repr(row)
        blob = bytes(row["ciphertext"])
        assert b"zen-test-key" not in blob
        assert row["fingerprint"]["key_prefix"] == "zen-"
        assert row["aad"]["connection_id"] == conn["id"]
        # Decryption under this AAD yields the payload — everything works.
        with SqlUnitOfWork(pg) as uow:
            material = svc.materialize(
                uow,
                workspace_id=workspace["workspace_id"],
                connection_id=conn["id"],
                purpose=PURPOSE_RUNTIME_EXECUTION,
            )
            assert material.payload == {"api_key": "zen-test-key"}

    def test_bad_payload_rejected(self, pg, svc, workspace):
        with SqlUnitOfWork(pg) as uow:
            with pytest.raises(DomainError) as ei:
                svc.create_connection(
                    uow,
                    workspace_id=workspace["workspace_id"],
                    principal_id=workspace["user_id"],
                    kind="opencode_zen",
                    label=None,
                    credential={"format": "api_key", "payload": {}},
                )
            assert ei.value.code == "validation_failed"

    def test_modal_kind_accepted(self, pg, svc, workspace):
        conn = _create(
            svc,
            pg,
            workspace,
            kind="modal",
            cred={"format": "token_pair", "payload": {"token_id": "ak-x", "token_secret": "as-y"}},
        )
        assert conn["kind"] == "modal"

    def test_validation_job_marks_ready(self, pg, svc, workspace, zen_connector):
        conn = _create(svc, pg, workspace)
        _drain(pg)
        with SqlUnitOfWork(pg) as uow:
            row = uow.connections.get(workspace["workspace_id"], conn["id"])
            assert row["health"] == "ready"
            assert (
                row["capability_observations"]["runtime_execution"]["preferred_model"]
                == "opencode/big-pickle"
            )
            obs = uow.connection_observations.list_for(workspace["workspace_id"], conn["id"])
            assert any(o["kind"] == "validation" for o in obs)
            # Plaintext never lands in observations.
            assert "zen-test-key" not in json.dumps(obs, default=str)

    def test_failed_validation_marks_reauth(self, pg, svc, workspace, zen_connector):
        _create(svc, pg, workspace, cred={"format": "api_key", "payload": {"api_key": "bad"}})
        _drain(pg)
        with SqlUnitOfWork(pg) as uow:
            conn = uow.rows.one(
                "SELECT * FROM connections WHERE workspace_id=%s",
                (workspace["workspace_id"],),
            )
            assert conn["health"] == "reauth_required"


class TestReplaceRevoke:
    def test_replace_rotates_version_and_epoch(self, pg, svc, workspace):
        conn = _create(svc, pg, workspace)
        old_cred = conn["current_credential_version_id"]
        with SqlUnitOfWork(pg) as uow:
            out = svc.replace_credential(
                uow,
                workspace_id=workspace["workspace_id"],
                connection_id=conn["id"],
                principal_id=workspace["user_id"],
                credential={"format": "api_key", "payload": {"api_key": "zen-NEW-key"}},
            )
        assert out["current_credential_version_id"] != old_cred
        assert out["revocation_epoch"] == 1
        with SqlUnitOfWork(pg) as uow:
            old = uow.credential_versions.get(workspace["workspace_id"], old_cred)
            assert old["state"] == "superseded"
            # Old ciphertext cannot be opened via the CURRENT pointer path.
            material = svc.materialize(
                uow,
                workspace_id=workspace["workspace_id"],
                connection_id=conn["id"],
                purpose=PURPOSE_RUNTIME_EXECUTION,
            )
            assert material.payload["api_key"] == "zen-NEW-key"

    def test_grant_redeem_is_one_shot_and_purpose_bound(self, pg, svc, workspace):
        conn = _create(svc, pg, workspace)
        with SqlUnitOfWork(pg) as uow:
            grant = svc.issue_grant(
                uow,
                workspace_id=workspace["workspace_id"],
                connection_id=conn["id"],
                purpose=PURPOSE_RUNTIME_EXECUTION,
                lease_id="lease_x",
            )
            # Wrong lease — denied.
            with pytest.raises(DomainError):
                svc.redeem_grant(
                    uow,
                    workspace_id=workspace["workspace_id"],
                    grant_id=grant["id"],
                    lease_id="lease_OTHER",
                )
            m = svc.redeem_grant(
                uow,
                workspace_id=workspace["workspace_id"],
                grant_id=grant["id"],
                lease_id="lease_x",
            )
            assert m.payload["api_key"] == "zen-test-key"
            with pytest.raises(DomainError) as ei:
                svc.redeem_grant(
                    uow,
                    workspace_id=workspace["workspace_id"],
                    grant_id=grant["id"],
                    lease_id="lease_x",
                )
            assert ei.value.code == "idempotency_conflict"

    def test_replace_fences_live_grants(self, pg, svc, workspace):
        conn = _create(svc, pg, workspace)
        with SqlUnitOfWork(pg) as uow:
            grant = svc.issue_grant(
                uow,
                workspace_id=workspace["workspace_id"],
                connection_id=conn["id"],
                purpose=PURPOSE_RUNTIME_EXECUTION,
            )
            gid = grant["id"]
            svc.replace_credential(
                uow,
                workspace_id=workspace["workspace_id"],
                connection_id=conn["id"],
                principal_id=workspace["user_id"],
                credential={"format": "api_key", "payload": {"api_key": "zen-2"}},
            )
            with pytest.raises(DomainError):
                svc.redeem_grant(uow, workspace_id=workspace["workspace_id"], grant_id=gid)


class TestLifecycle:
    def test_disable_blocks_materialize(self, pg, svc, workspace):
        conn = _create(svc, pg, workspace)
        with SqlUnitOfWork(pg) as uow:
            svc.disable(uow, workspace_id=workspace["workspace_id"], connection_id=conn["id"])
        with SqlUnitOfWork(pg) as uow:
            with pytest.raises(DomainError) as ei:
                svc.issue_grant(
                    uow,
                    workspace_id=workspace["workspace_id"],
                    connection_id=conn["id"],
                    purpose=PURPOSE_RUNTIME_EXECUTION,
                )
            assert ei.value.code == "invalid_state"

    def test_disconnect_tombstone(self, pg, svc, workspace):
        conn = _create(svc, pg, workspace)
        with SqlUnitOfWork(pg) as uow:
            out = svc.disconnect(
                uow,
                workspace_id=workspace["workspace_id"],
                connection_id=conn["id"],
                principal_id=workspace["user_id"],
            )
        assert out["state"] == "revoked"
        with SqlUnitOfWork(pg) as uow:
            # Tombstone visible via include_revoked; excluded otherwise.
            names = [
                c["state"]
                for c in svc.list_connections(uow, workspace_id=workspace["workspace_id"])
            ]
            assert names == []
            names = [
                c["state"]
                for c in svc.list_connections(
                    uow, workspace_id=workspace["workspace_id"], include_revoked=True
                )
            ]
            assert names == ["revoked"]
            creds = uow.rows.all(
                "SELECT state FROM credential_versions WHERE connection_id=%s", (conn["id"],)
            )
            assert all(c["state"] == "revoked" for c in creds)

    def test_disconnect_rejected_while_execution_live(self, pg, svc, workspace):
        conn = _create(svc, pg, workspace)
        with SqlUnitOfWork(pg) as uow:
            # Simulate a live execution bound to a grant on this connection.
            from control.domain import ids as _ids

            sid = _ids.new_id("session")
            uow.conn.execute(
                "INSERT INTO sessions (id, workspace_id, role, lifecycle,"
                " harness, effective_input_digest, created_by) VALUES"
                " (%s,%s,'author','open','{}','sha256:x',%s)",
                (sid, workspace["workspace_id"], workspace["user_id"]),
            )
            mid = _ids.new_id("message")
            uow.conn.execute(
                "INSERT INTO messages (id, workspace_id, session_id, ordinal,"
                " author, routing, content) VALUES (%s,%s,%s,1,'{}','queue','{}')",
                (mid, workspace["workspace_id"], sid),
            )
            tid = _ids.new_id("turn")
            eid = _ids.new_id("execution")
            uow.conn.execute(
                "INSERT INTO turns (id, workspace_id, session_id, ordinal,"
                " message_id, state) VALUES (%s,%s,%s,1,%s,'started')",
                (tid, workspace["workspace_id"], sid, mid),
            )
            uow.conn.execute(
                "INSERT INTO executions (id, workspace_id, session_id, turn_id,"
                " attempt_ordinal, operation_id, state) VALUES (%s,%s,%s,%s,1,%s,'started')",
                (eid, workspace["workspace_id"], sid, tid, _ids.new_id("effect")),
            )
            svc.issue_grant(
                uow,
                workspace_id=workspace["workspace_id"],
                connection_id=conn["id"],
                purpose=PURPOSE_RUNTIME_EXECUTION,
                session_id=sid,
                execution_id=eid,
            )
            with pytest.raises(DomainError) as ei:
                svc.disconnect(
                    uow,
                    workspace_id=workspace["workspace_id"],
                    connection_id=conn["id"],
                    principal_id=workspace["user_id"],
                )
            assert "live executions" in str(ei.value)
            uow.commit()


class TestIsolationAndRestart:
    def test_cross_owner_404(self, pg, svc, workspace):
        conn = _create(svc, pg, workspace)
        with SqlUnitOfWork(pg) as uow:
            other_ws = "wsp_01aaaaaaaaaaaaaaaaaaaaaaaaaa"
            uow.conn.execute(
                "INSERT INTO users (id, email_normalized)"
                " VALUES ('usr_01bbbbbbbbbbbbbbbbbbbbbbbbbb','o@x.dev')"
            )
            uow.workspaces.insert(
                {
                    "id": other_ws,
                    "owner_user_id": "usr_01bbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "name": "personal",
                }
            )
            with pytest.raises(DomainError) as ei:
                svc.get_connection(uow, workspace_id=other_ws, connection_id=conn["id"])
            assert ei.value.code == "not_found"
            # Grants can't be minted cross-owner either.
            with pytest.raises(DomainError):
                svc.issue_grant(
                    uow,
                    workspace_id=other_ws,
                    connection_id=conn["id"],
                    purpose=PURPOSE_RUNTIME_EXECUTION,
                )

    def test_durability_across_uow_cycles(self, pg, svc, workspace):
        """Restart durability: ciphertext + metadata survive; reopening works
        in a fresh transaction (control-plane restart equivalent)."""
        conn = _create(svc, pg, workspace)
        with SqlUnitOfWork(pg) as uow:
            row = svc.get_connection(
                uow, workspace_id=workspace["workspace_id"], connection_id=conn["id"]
            )
            assert row["kind"] == "opencode_zen"
            material = svc.materialize(
                uow,
                workspace_id=workspace["workspace_id"],
                connection_id=conn["id"],
                purpose=PURPOSE_RUNTIME_EXECUTION,
            )
            assert material.payload["api_key"] == "zen-test-key"


class TestModelDiscovery:
    def test_catalog_prefers_free(self, pg, svc, vault, workspace, zen_connector):
        _create(svc, pg, workspace)
        models = ModelDiscoveryService(pg, svc, _reg(zen_connector))
        with SqlUnitOfWork(pg) as uow:
            listed = models.list_models(uow, workspace_id=workspace["workspace_id"])
        ids = [m.id for m in listed]
        assert ids[0] == "opencode/big-pickle"  # free first
        assert "opencode/paid-1" in ids
        with SqlUnitOfWork(pg) as uow:
            default = models.pick_default(uow, workspace_id=workspace["workspace_id"])
            assert default.id == "opencode/big-pickle"
            assert default.free

    def test_no_connection_no_models(self, pg, svc, vault, workspace, zen_connector):
        models = ModelDiscoveryService(pg, svc, _reg(zen_connector))
        with SqlUnitOfWork(pg) as uow:
            with pytest.raises(DomainError) as ei:
                models.pick_default(uow, workspace_id=workspace["workspace_id"])
            assert ei.value.code == "executor_unavailable"


def _reg(zen_connector):
    reg = ConnectorRegistry()
    reg.register(zen_connector)
    return reg

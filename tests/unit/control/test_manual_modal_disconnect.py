"""SOR-295 P1: disconnect must preserve owner compute teardown authority."""

import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from control.app import create_app
from control.auth_store import AuthDatabase, AuthStore, PersistentApiKeyStore
from control.backend import SandboxSpec
from control.connections import SecretVault
from control.hosted_auth import HostedAuthError
from control.hosted_compute import FakeComputeProvider, HostedModalBackend
from control.manual_connections import ManualConnections
from control.modal_connection import FakeModalProvider
from control.store import SessionRecord
from fastapi.testclient import TestClient


@pytest.fixture
def modal_setup(tmp_path):
    path = tmp_path / "auth.db"
    # Existing opt-in accepts only a disposable local PostgreSQL database.
    url = os.environ.get("SBX_TEST_MANUAL_POSTGRES_URL")

    def auth_factory():
        return AuthStore(AuthDatabase(database_url=url) if url else AuthDatabase(path=path))

    auth = auth_factory()
    users = [auth.create_user() for _ in range(2)]
    vault = SecretVault(secrets.token_bytes(32))
    provider = FakeComputeProvider()

    def factory():
        return create_app(
            auth_store=auth_factory(),
            hosted=True,
            state_backend="postgres",
            connection_vault=vault,
            modal_provider=FakeModalProvider(),
            compute_provider=provider,
        )

    app = factory()
    for user in users:
        app.state.connections.connect(
            user.id, "modal", {"token_id": "REDACTED", "token_secret": "REDACTED"}
        )
        app.state.modal_connections.provision(user.id)
    headers = [
        {
            "Authorization": f"Bearer {PersistentApiKeyStore(auth).create(user_id=u.id)[1]}",
            "Content-Type": "application/json",
        }
        for u in users
    ]
    return app, factory, users, headers, provider


def spec(owner, agent="agent"):
    return SandboxSpec(tags={"owner": owner, "session_id": agent, "provider": "opencode"})


def backend(app):
    return HostedModalBackend(app.state.connections, app.state.compute_provider)


@pytest.mark.parametrize("state", ["live", "retained"])
def test_disconnect_refuses_owner_compute_and_preserves_teardown_after_restart(modal_setup, state):
    app, factory, users, headers, _ = modal_setup
    compute = backend(app)
    agent = "agent-" + users[0].id
    handle = compute.create(spec(users[0].id, agent))
    data = compute.records.get("hosted_sandboxes", handle.id, owner=users[0].id)
    data["state"] = state
    compute.records.put_owned("hosted_sandboxes", handle.id, users[0].id, data)
    now = datetime.now(UTC)
    app.state.plane.store.put(
        SessionRecord(
            agent,
            "test",
            "idle",
            now,
            now,
            "opencode/free",
            0,
            None,
            [],
            users[0].id,
            sandbox_id=handle.id,
            sandbox_root=str(handle.root),
            sandbox_tags=handle.tags,
        )
    )
    before = app.state.connections.get(users[0].id, "modal")
    # A fabricated owner tag cannot confer authority over the real resource.
    forged = replace(handle, tags={**handle.tags, "owner": users[1].id})
    for operation in (compute.poll, compute.terminate):
        with pytest.raises(HostedAuthError, match="sandbox_not_found"):
            operation(forged)
    restored = factory()
    with TestClient(restored) as client:
        for method in (client.get, client.delete):
            assert method(f"/v1/agents/{agent}", headers=headers[1]).status_code == 404
        refused = client.delete("/hosted/connections/modal", headers=headers[0])
        assert refused.status_code == 409
        assert "modal_resources_require_cleanup_before_disconnect" in refused.text
        assert restored.state.connections.get(users[0].id, "modal") == before
        # B disconnects only B's credential; A's compute remains manageable.
        assert client.delete("/hosted/connections/modal", headers=headers[1]).status_code == 200
        compute = backend(restored)
        assert compute.poll(handle).alive
        assert handle.id in {h.id for h in compute.list({"owner": users[0].id})}
        assert compute.list({"owner": users[1].id}) == []
        compute.terminate(handle)
        assert not compute.poll(handle).alive
        assert client.delete("/hosted/connections/modal", headers=headers[0]).status_code == 200
        status = client.get("/hosted/connections/modal", headers=headers[0]).json()["connection"]
        assert status["state"] == "disabled" and status["metadata"] == {}
    disabled = factory().state.connections.get(users[0].id, "modal")
    assert disabled.state == "disabled" and disabled.credential_cipher is None


def test_normal_disconnect_without_compute_is_durable(modal_setup):
    app, factory, users, headers, _ = modal_setup
    with TestClient(app) as client:
        assert client.delete("/hosted/connections/modal", headers=headers[0]).status_code == 200
    record = factory().state.connections.get(users[0].id, "modal")
    assert record.state == "disabled" and record.credential_cipher is None and record.metadata == {}
    with pytest.raises(HostedAuthError, match="modal_runtime_required"):
        backend(factory()).create(spec(users[0].id))


@pytest.mark.parametrize("restore", [False, True])
def test_inflight_create_or_restore_blocks_disconnect_across_instances(modal_setup, restore):
    app, factory, users, _, provider = modal_setup
    compute = backend(app)
    entered, finish = threading.Event(), threading.Event()
    original = provider.create

    def blocked(context, declaration, runtime):
        entered.set()
        assert finish.wait(5)
        return original(context, declaration, runtime)

    if restore:
        snapshot = "snapshot-" + users[0].id
        compute.records.put_owned(
            "hosted_snapshots",
            snapshot,
            users[0].id,
            {
                "agent_id": "agent",
                "connection_id": app.state.connections.get(users[0].id, "modal").id,
            },
        )
        provider.restore = blocked

        def operation():
            return compute.restore(snapshot, spec(users[0].id))
    else:
        provider.create = blocked

        def operation():
            return compute.create(spec(users[0].id))

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(operation)
        try:
            assert entered.wait(5)
            restored = factory()
            with pytest.raises(HostedAuthError, match="cleanup_before_disconnect"):
                restored.state.manual_connections.disable(users[0].id, "modal")
            assert backend(restored).records.rows("hosted_sandbox_creates", owner=users[0].id)
        finally:
            finish.set()
        handle = pending.result(timeout=5)
    assert compute.records.rows("hosted_sandbox_creates", owner=users[0].id) == []
    backend(factory()).terminate(handle)
    assert factory().state.manual_connections.disable(users[0].id, "modal").state == "disabled"


def test_ambiguous_create_preserves_durable_authority(modal_setup):
    app, factory, users, _, provider = modal_setup
    original = provider.create
    created = []

    def failed(*args):
        created.append(original(*args))
        raise HostedAuthError("modal_provider_unavailable", 503)

    provider.create = failed
    with pytest.raises(HostedAuthError, match="modal_provider_unavailable"):
        backend(app).create(spec(users[0].id))
    restored = factory()
    with pytest.raises(HostedAuthError, match="cleanup_before_disconnect"):
        restored.state.manual_connections.disable(users[0].id, "modal")
    assert restored.state.connections.credentials(
        restored.state.connections.get(users[0].id, "modal")
    ) == {
        "token_id": "REDACTED",
        "token_secret": "REDACTED",
    }
    compute = backend(restored)
    assert created[0].id in {h.id for h in compute.list({"owner": users[0].id})}
    # Operator reconciliation uses the retained, owner-bound authority even
    # when the remote create response (and hence the final record) was lost.
    context, _ = compute._context(users[0].id)
    provider.terminate(context, created[0])
    for claim, _ in compute.records.rows("hosted_sandbox_creates", owner=users[0].id):
        compute.records.delete("hosted_sandbox_creates", claim, owner=users[0].id)
    assert restored.state.manual_connections.disable(users[0].id, "modal").state == "disabled"


def test_partial_teardown_failure_does_not_allow_disconnect(modal_setup):
    app, factory, users, _, provider = modal_setup
    compute = backend(app)
    handles = [compute.create(spec(users[0].id, f"agent-{i}")) for i in range(2)]
    compute.terminate(handles[0])
    original = provider.terminate

    def failed(*args):
        raise HostedAuthError("modal_provider_unavailable", 503)

    provider.terminate = failed
    with pytest.raises(HostedAuthError, match="modal_provider_unavailable"):
        compute.terminate(handles[1])
    restored = factory()
    with pytest.raises(HostedAuthError, match="cleanup_before_disconnect"):
        restored.state.manual_connections.disable(users[0].id, "modal")
    assert backend(restored).poll(handles[1]).alive
    provider.terminate = original
    backend(restored).terminate(handles[1])
    assert restored.state.manual_connections.disable(users[0].id, "modal").state == "disabled"


@pytest.mark.parametrize("upgrading", [False, True])
def test_runtime_provisioning_blocks_disconnect_even_after_lease_expiry(modal_setup, upgrading):
    app, factory, users, _, _ = modal_setup
    record = app.state.connections.get(users[0].id, "modal")
    if not upgrading:
        record.state = "provisioning"
    record.metadata["lease_until"] = 1
    app.state.connections.save(record)
    restored = factory()
    service = ManualConnections(restored.state.connections)
    with pytest.raises(HostedAuthError, match="provisioning_in_progress_retry_disconnect"):
        service.disable(users[0].id, "modal")
    assert restored.state.connections.get(users[0].id, "modal") == record

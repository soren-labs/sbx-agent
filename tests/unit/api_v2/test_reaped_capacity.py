"""The task filter must release web-process leases after remote reaping."""

import time
from datetime import UTC, datetime

import pytest
from control.scheduler import AccountScheduler, session_running_source
from control.store import SessionRecord
from tests.unit.api_v2.conftest import wait_session


@pytest.mark.parametrize("account_id", ["auto", "acct-codex-1"])
def test_create_releases_reaped_capacity_before_account_filter(client, v1_env, auth, account_id):
    store = v1_env.store
    scheduler = AccountScheduler(
        v1_env.registry,
        providers=("codex",),
        max_global=1,
        external_running=session_running_source(store),
    )
    v1_env.app.state.scheduler = scheduler
    state = v1_env.app.state.v1_state
    now = datetime.now(UTC)
    lease = scheduler.acquire(provider="codex", account="acct-codex-1")
    state.set_lease("reaped", lease)
    store.put(
        SessionRecord(
            id="reaped",
            title="reaped",
            status="suspended",
            created_at=now,
            updated_at=now,
            model="gpt-5.6-luna",
            turns=1,
            usage=None,
            messages=[],
            owner=v1_env.agents_key_id,
            sandbox_tags={"account_id": "acct-codex-1", "provider": "codex"},
        )
    )
    # A previous bind can have reconciled just before the remote cron
    # suspended the agent. Its ten-second throttle must not reject creates.
    state.leases_reconciled_at = time.monotonic()
    assert scheduler.running_count("acct-codex-1") == 1

    response = client.post(
        "/v2/sessions",
        headers=auth,
        json={
            "prompt": "Create hello.txt.",
            "execution": {"provider": "codex", "account_id": account_id},
        },
    )
    assert response.status_code == 201, response.text
    sid = response.json()["session"]["id"]
    detail = wait_session(client, auth, sid, "finished", "failed")
    assert detail["session"]["status"] == "finished", detail
    assert lease.released
    assert "reaped" not in state.leases
    assert scheduler.active_count == 1

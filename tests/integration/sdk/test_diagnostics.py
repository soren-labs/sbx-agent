"""SBXClient.diagnose / `sbx status`: one category per failure, against the real API."""

from __future__ import annotations

import pytest
from tests.support.api import ApiStack

from sbx.sdk import SBXClient

EMAIL, PASSWORD = "diag@example.test", "diag password 12345"


class _ModalStandIn:
    """Enabled-but-credential-less Modal backend: only what /api/executor-backends reads."""

    def capabilities(self) -> dict[str, object]:
        return {"backend": "modal", "snapshot": "filesystem", "native_pause": False}


def _signed_in(stack: ApiStack) -> tuple[SBXClient, dict]:
    client = SBXClient("http://testserver", http=stack.client())
    client.register(EMAIL, PASSWORD)
    client.verify_email(stack.services.mailer.latest(EMAIL)["body"].split("token=")[1].strip())
    client.login(EMAIL, PASSWORD)
    key = client.create_api_key("diag", ["*"])
    return SBXClient("http://testserver", key["key"], http=stack.client()), key


@pytest.fixture
def stack(db, tmp_path):
    s = ApiStack(db, tmp_path)
    yield s
    s.shutdown()


def test_healthy_deployment_reports_usable_executors(stack) -> None:
    client, _ = _signed_in(stack)
    report = client.diagnose()
    assert report["ok"] is True and report["problem"] is None
    assert report["executor_backends"] == ["local"]
    assert [c["category"] for c in report["checks"]] == [
        "reachability",
        "reachability",
        "authentication",
        "executor",
    ]


def test_missing_and_revoked_keys_are_authentication_failures(stack) -> None:
    anonymous = SBXClient("http://testserver", http=stack.client())
    missing = anonymous.diagnose()
    assert missing["problem"]["category"] == "authentication"
    assert "sbx auth login" in missing["problem"]["action"]

    client, key = _signed_in(stack)
    client.revoke_api_key(key["id"])
    revoked = client.diagnose()
    assert revoked["problem"]["category"] == "authentication"
    assert revoked["problem"]["name"] == "api_key" and "rejected" in revoked["problem"]["message"]


def test_not_ready_database_is_reachability(stack, monkeypatch) -> None:
    client, _ = _signed_in(stack)

    def unreadable(fn):
        raise RuntimeError("database down")

    with monkeypatch.context() as patch:
        patch.setattr(stack.services.tx, "read", unreadable)
        report = client.diagnose()
    assert report["problem"]["category"] == "reachability"
    assert report["problem"]["name"] == "database"


def test_modal_without_a_ready_connection_is_an_executor_failure(db, tmp_path) -> None:
    stack = ApiStack(db, tmp_path, executors={"modal": _ModalStandIn()})
    client, _ = _signed_in(stack)
    report = client.diagnose()
    assert report["problem"]["category"] == "executor"
    assert report["executor_backends"] == ["modal"]
    hints = {c["name"]: c for c in report["checks"]}
    assert "sbx connections add modal" in hints["executor_modal"]["action"]

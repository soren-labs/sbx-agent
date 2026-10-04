import json

import httpx
import pytest
from control.auth_email import EmailDeliveryUnavailable
from control.resend_email import ResendEmailSender


def sender(handler, **kwargs):
    return ResendEmailSender(
        "REDACTED",
        "SBX Agent <verify@sbx-agent.com>",
        client=httpx.Client(
            base_url="https://api.resend.com", transport=httpx.MockTransport(handler)
        ),
        **kwargs,
    )


def test_delivery_uses_server_key_and_safe_sender():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "message-id"})

    service = sender(handle, reply_to="support@sbx-agent.com")
    service.send_verification(email="test@example.test", code="123456", expires_in_s=600)
    request = requests[0]
    body = json.loads(request.content)
    assert request.url.path == "/emails"
    assert request.headers["Authorization"] == "Bearer REDACTED"
    assert body["reply_to"] == "support@sbx-agent.com"
    assert body["from"] == "SBX Agent <verify@sbx-agent.com>"
    assert "123456" in body["text"] and "10 minutes" in body["text"]
    assert "123456" not in body["subject"]
    assert "REDACTED" not in repr(service)


@pytest.mark.parametrize("status", [301, 400, 401, 403, 429, 500])
def test_provider_body_never_escapes(status, caplog):
    service = sender(lambda _: httpx.Response(status, text="REDACTED provider body"))
    with pytest.raises(EmailDeliveryUnavailable) as caught:
        service.send_verification(email="test@example.test", code="123456", expires_in_s=600)
    assert str(caught.value) == "verification email delivery unavailable"
    assert "REDACTED" not in caplog.text


def test_network_and_malformed_success_are_safe():
    def timeout(_):
        raise httpx.ReadTimeout("REDACTED")

    for service in (sender(timeout), sender(lambda _: httpx.Response(200, json={}))):
        with pytest.raises(EmailDeliveryUnavailable):
            service.send_verification(email="test@example.test", code="123456", expires_in_s=600)
        assert service.preflight()["ready"] is False


@pytest.mark.parametrize(
    "address",
    [
        "verify@attacker.test",
        "verify@sbx-agent.com\r\nBcc: attacker@test.com",
        "verify@sbx-agent.com,attacker@test.com",
        "Spoof, attacker@test.com <verify@sbx-agent.com>",
    ],
)
def test_unsafe_sender_rejected(address):
    with pytest.raises(ValueError):
        ResendEmailSender("REDACTED", address)


def test_preflight_checks_verified_domain_without_returning_provider_data():
    service = sender(
        lambda _: httpx.Response(
            200,
            json={
                "data": [
                    {"name": "sbx-agent.com", "status": "verified", "secret": "REDACTED"},
                ]
            },
        )
    )
    assert service.preflight() == {
        "provider": "resend",
        "domain": "sbx-agent.com",
        "ready": True,
        "error": None,
    }


def test_production_mode_selects_resend_without_network(monkeypatch):
    from control.app import create_app

    monkeypatch.setenv("SBX_AUTH_EMAIL_MODE", "production")
    monkeypatch.setenv("RESEND_API_KEY", "REDACTED")
    monkeypatch.setenv("SBX_AUTH_EMAIL_FROM", "verify@sbx-agent.com")
    assert isinstance(create_app().state.hosted_auth.sender, ResendEmailSender)


def test_missing_production_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with pytest.raises(ValueError, match="configuration is incomplete"):
        ResendEmailSender.from_env()

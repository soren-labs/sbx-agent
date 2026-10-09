"""Generic inference connector: per-protocol probes, catalog and outbound-URL policy."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from control.domain.errors import DomainError
from control.integrations.connectors import inference_api

KEY = "byok-connector-key-0123456789"
CHAT, RESPONSES, ANTHROPIC = (
    "https://93.184.216.34/v1",
    "https://93.184.216.34/responses/v1",
    "https://93.184.216.34/anthropic",
)


class Reply:
    def __init__(self, status: int, body: Any = None, headers: dict | None = None) -> None:
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self) -> Any:
        return self._body


class Provider:
    """Records what the connector sends and answers from a route table."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes, self.calls = routes, []

    def _answer(self, method: str, url: str, **kw: Any) -> Reply:
        self.calls.append({"method": method, "url": url, **kw})
        assert kw["follow_redirects"] is False, "redirects are never followed"
        answer = self.routes.get(url, Reply(404))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def post(self, url: str, **kw: Any) -> Reply:
        return self._answer("POST", url, **kw)

    def get(self, url: str, **kw: Any) -> Reply:
        return self._answer("GET", url, **kw)


def material(**endpoints: str) -> dict[str, Any]:
    return inference_api.normalize(
        {"api_key": KEY, "model": "vendor-model", "endpoints": endpoints or {"openai_chat": CHAT}}
    )


def test_each_protocol_is_probed_with_its_own_request_shape_and_auth() -> None:
    provider = Provider(
        {
            f"{CHAT}/chat/completions": Reply(200),
            f"{RESPONSES}/responses": Reply(200),
            f"{ANTHROPIC}/v1/messages": Reply(200),
            f"{CHAT}/models": Reply(200, {"data": [{"id": "vendor-model"}, {"id": "vendor-big"}]}),
        }
    )
    observation = inference_api.validate(
        material(openai_chat=CHAT, openai_responses=RESPONSES, anthropic_messages=ANTHROPIC),
        client=provider,
    )
    assert observation.status == "ready" and observation.quota_consuming is True
    posts = {c["url"]: c for c in provider.calls if c["method"] == "POST"}
    chat, responses, anthropic = (
        posts[f"{CHAT}/chat/completions"],
        posts[f"{RESPONSES}/responses"],
        posts[f"{ANTHROPIC}/v1/messages"],
    )
    assert chat["headers"]["Authorization"] == f"Bearer {KEY}"
    assert chat["json"]["model"] == "vendor-model" and chat["json"]["max_tokens"] == 1
    assert responses["headers"]["Authorization"] == f"Bearer {KEY}"
    assert responses["json"]["input"] == "ping"
    assert anthropic["headers"]["x-api-key"] == KEY and "Authorization" not in anthropic["headers"]
    assert anthropic["headers"]["anthropic-version"]
    assert observation.catalog == {
        "models": [{"id": "vendor-model"}, {"id": "vendor-big"}],
        "preferred_model": "vendor-model",
        "protocols": ["openai_chat", "openai_responses", "anthropic_messages"],
        "source": "configured + provider model list",
    }
    assert set(observation.details["endpoints"]) == set(observation.catalog["protocols"])
    assert KEY not in str(observation.details) + str(observation.catalog)


@pytest.mark.parametrize(
    "reply,status,reason",
    [
        (Reply(401), "invalid", "inference_rejected_key"),
        (Reply(403), "invalid", "inference_rejected_key"),
        (Reply(404), "invalid", "inference_endpoint_or_model_rejected"),
        (Reply(400), "invalid", "inference_endpoint_or_model_rejected"),
        (
            Reply(302, headers={"location": "http://10.0.0.1/"}),
            "invalid",
            "inference_base_url_redirects",
        ),
        (Reply(429, headers={"retry-after": "7"}), "degraded", "inference_rate_limited"),
        (Reply(503), "error", "inference_unexpected"),
        (httpx.ConnectError("boom"), "error", "inference_unreachable:ConnectError"),
    ],
)
def test_probe_outcomes_are_classified(reply, status, reason) -> None:
    provider = Provider({f"{CHAT}/chat/completions": reply})
    observation = inference_api.validate(material(), client=provider)
    assert (observation.status, observation.details["reason"]) == (status, reason)
    assert observation.catalog is None, "no catalog is published for an unverified credential"
    if status == "degraded":
        assert observation.retry_after == 7.0


def test_one_bad_endpoint_fails_the_whole_connection() -> None:
    provider = Provider(
        {f"{CHAT}/chat/completions": Reply(200), f"{ANTHROPIC}/v1/messages": Reply(404)}
    )
    observation = inference_api.validate(
        material(openai_chat=CHAT, anthropic_messages=ANTHROPIC), client=provider
    )
    assert observation.status == "invalid"
    assert observation.details["endpoints"]["openai_chat"]["status"] == "ready"
    assert observation.details["endpoints"]["anthropic_messages"]["status"] == "invalid"


def test_catalog_falls_back_to_configured_models_when_listing_is_unavailable() -> None:
    provider = Provider({f"{CHAT}/chat/completions": Reply(200)})
    credential = inference_api.normalize(
        {"api_key": KEY, "model": "m1", "models": ["m2", "m1"], "base_url": CHAT}
    )
    catalog = inference_api.validate(credential, client=provider).catalog
    assert catalog["models"] == [{"id": "m1"}, {"id": "m2"}] and catalog["source"] == "configured"


def test_hostname_resolving_to_a_private_address_is_never_contacted(monkeypatch) -> None:
    def resolve(host: str, port: int, **_: Any) -> list:
        return [(2, 1, 6, "", ("10.1.2.3", port))]

    monkeypatch.setattr(inference_api.socket, "getaddrinfo", resolve)
    provider = Provider({})
    credential = inference_api.normalize(
        {"api_key": KEY, "model": "m", "base_url": "https://rebind.example.test/v1"}
    )
    observation = inference_api.validate(credential, client=provider)
    assert observation.status == "invalid"
    assert observation.details["reason"] == "inference_base_url_not_public"
    assert provider.calls == []


def test_private_targets_need_the_operator_opt_in(monkeypatch) -> None:
    private = {"api_key": KEY, "model": "m", "base_url": "http://10.0.0.5:8080/v1"}
    with pytest.raises(DomainError):
        inference_api.normalize(private)
    monkeypatch.setenv("SBX_INFERENCE_ALLOW_PRIVATE_URLS", "1")
    assert inference_api.normalize(private)["endpoints"] == {
        "openai_chat": "http://10.0.0.5:8080/v1"
    }


def test_normalize_canonicalizes_and_exposes_only_public_settings() -> None:
    credential = inference_api.normalize(
        {
            "api_key": f"  {KEY}  ",
            "model": "vendor/model:free",
            "base_url": "https://API.Example.Test:8443/v1/",
            "protocol": "openai_responses",
        }
    )
    assert credential["api_key"] == KEY
    assert credential["endpoints"] == {"openai_responses": "https://api.example.test:8443/v1"}
    public = inference_api.public_config(credential)
    assert "api_key" not in public and KEY not in str(public)
    assert set(inference_api.SECRET_FIELDS) == set(credential) - set(public)

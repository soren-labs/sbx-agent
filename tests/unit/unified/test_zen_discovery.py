import httpx
from control.integrations.connectors.opencode_zen import ZenConnector


def test_catalog_prefers_observed_free_cost_and_does_not_call_model_api():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if request.url.host == "opencode.ai":
            assert request.headers["Authorization"] == "Bearer REDACTED"
            return httpx.Response(200, json={"data": [{"id": "paid"}, {"id": "free"}]})
        return httpx.Response(
            200,
            json={
                "opencode": {
                    "models": {
                        "paid": {"cost": {"input": 1, "output": 2}},
                        "free": {"cost": {"input": 0, "output": 0}},
                    }
                }
            },
        )

    result = ZenConnector(httpx.MockTransport(respond)).validate({"api_key": "REDACTED"})
    assert result["models"][0]["id"] == "opencode/free"
    assert result["models"][0]["free"]
    assert result["inference_verified"] is False
    assert all("/models" in url or "models.dev" in url for url in calls)

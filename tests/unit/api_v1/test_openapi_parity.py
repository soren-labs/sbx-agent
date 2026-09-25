"""SOR-226: the served ``/v1/openapi.json`` is runtime-derived and in strict
parity with ``docs/contracts/api-v1.yaml`` — same ops, same canonical
constants, same error body shape — and the error contract stays /v1
backward compatible (additive fields only)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from control.api_v1.error_catalog import (
    ERROR_ACTIONS,
    ERROR_CATALOG,
    ERROR_SUBCODES,
    spec_for,
)
from control.api_v1.openapi import error_body_schema
from fastapi.testclient import TestClient

CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "api-v1.yaml"
CONTROL = Path(__file__).resolve().parents[3] / "control"
HTTP_METHODS = ("get", "post", "put", "delete", "patch")


def _normalize(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _ops(paths: dict[str, Any]) -> set[tuple[str, str]]:
    ops: set[tuple[str, str]] = set()
    for path, item in paths.items():
        for method, spec in item.items():
            if method in HTTP_METHODS and isinstance(spec, dict):
                ops.add((method, _normalize(path)))
    return ops


def _contract() -> dict[str, Any]:
    data = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _generated(client: TestClient) -> dict[str, Any]:
    resp = client.get("/v1/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert isinstance(spec, dict)
    return spec


def test_openapi_endpoint_serves_valid_spec(client: TestClient) -> None:
    spec = _generated(client)
    assert spec["info"]["title"] == "sbx-browser Public API v1"
    assert spec["info"]["version"] == "1.0.0"
    assert str(spec["openapi"]).startswith("3.1")
    assert spec["security"] == [{"bearerAuth": []}]
    schemes = spec["components"]["securitySchemes"]
    assert schemes["bearerAuth"] == {"type": "http", "scheme": "bearer"}


def test_ops_parity_contract_runtime_generated(client: TestClient) -> None:
    contract = _contract()
    generated = _generated(client)
    runtime = _ops({p: i for p, i in client.app.openapi()["paths"].items() if p.startswith("/v1")})
    declared = _ops(contract["paths"])
    served = _ops(generated["paths"])
    assert declared == served == runtime


def test_http_paths_canonical_matches_ops(client: TestClient) -> None:
    contract = _contract()
    http_paths = {
        (entry.split()[0].lower(), _normalize(entry.split(None, 1)[1]))
        for entry in contract["x-canonical"]["http_paths"]
    }
    assert http_paths == _ops(contract["paths"])


def test_x_canonical_parity(client: TestClient) -> None:
    contract_canon = _contract()["x-canonical"]
    runtime_canon = _generated(client)["x-canonical"]
    for key in (
        "error_codes",
        "error_subcodes",
        "error_actions",
        "run_error_codes",
        "run_error_sources",
        "providers",
        "keepalives_s",
        "sse",
        "paths",
        "exit_codes",
        "commands",
        "codex_events",
        "item_types",
        "runner_events",
        "usage_fields",
        "usage_fields_optional",
    ):
        left, right = contract_canon[key], runtime_canon[key]
        if isinstance(left, (list, dict)):
            assert set(_seq(left)) == set(_seq(right)), key
        else:
            assert left == right, key


def _seq(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [f"{k}:{v}" for k, v in sorted(value.items())]
    return list(value)


def test_error_body_schema_parity(client: TestClient) -> None:
    contract_body = _contract()["components"]["schemas"]["ErrorBody"]
    generated_body = _generated(client)["components"]["schemas"]["ErrorBody"]
    assert generated_body == contract_body == error_body_schema()


def test_error_body_codes_and_actions_cover_catalog() -> None:
    body = error_body_schema()["properties"]["error"]
    assert set(body["required"]) == {"code", "message", "retryable", "action"}
    assert body["properties"]["code"]["enum"] == list(ERROR_SUBCODES)
    assert set(body["properties"]["action"]["enum"]) == set(ERROR_ACTIONS)


def test_no_validation_422_leaks(client: TestClient) -> None:
    spec = _generated(client)
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            assert "422" not in operation["responses"], f"{method} {path}"
    schemas = spec["components"]["schemas"]
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas


def test_every_error_response_is_error_body(client: TestClient) -> None:
    """Every non-2xx response in the generated spec is the canonical body."""
    spec = _generated(client)
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            for status, response in operation["responses"].items():
                if not status.isdigit() or int(status) < 400:
                    continue
                schema = response.get("content", {}).get("application/json", {}).get("schema")
                assert schema == {"$ref": "#/components/schemas/ErrorBody"}, (
                    f"{method} {path} {status}: {schema}"
                )


def test_catalog_covers_every_raised_code() -> None:
    """Every literal error code raised under control/api_v1 is catalogued."""
    raised: set[str] = set()
    pattern = re.compile(
        r"(?:V1ApiError|error_body|invalid_request|invalid_provider|not_found|"
        r"unauthorized|forbidden|_v1_error)\(\s*\d+\s*,\s*[\"']([a-z_]+)[\"']"
    )
    named = re.compile(r"[\"']([a-z_]+)[\"']\s*,\s*[\"']")
    for path in CONTROL.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        raised.update(pattern.findall(text))
        for match in re.finditer(r"V1ApiError\(\s*\d+\s*,", text):
            tail = text[match.end() : match.end() + 60]
            found = named.search(tail)
            if found:
                raised.add(found.group(1))
    missing = raised - set(ERROR_CATALOG)
    assert not missing, f"codes raised but missing from ERROR_CATALOG: {missing}"


def test_spec_for_fallback_and_semantics() -> None:
    auth = spec_for("unauthorized", 401)
    assert auth.retryable is False and auth.action == "authenticate"
    busy = spec_for("turn_in_progress", 409)
    assert busy.retryable is True and busy.action == "wait"
    gone = spec_for("not_found", 404)
    assert gone.retryable is False and gone.action == "lookup"
    exhausted = spec_for("provider_exhausted", 429)
    assert exhausted.retryable is True and exhausted.action == "retry"
    # Unknown codes synthesize a safe fallback instead of crashing.
    transient = spec_for("totally_unknown", 503)
    assert transient.retryable is True and transient.action == "retry"
    bad = spec_for("totally_unknown", 400)
    assert bad.retryable is False and bad.action == "fix_request"


def test_v1_error_body_backward_compatible(client: TestClient) -> None:
    """Old fields (code/message/retry_after) still present; new are additive."""
    resp = client.get("/v1/agents")
    assert resp.status_code == 401
    error = resp.json()["error"]
    assert {"code", "message"} <= set(error)  # pre-SOR-226 fields
    assert {"retryable", "action"} <= set(error)  # additive SOR-226 fields
    assert error["code"] in ERROR_SUBCODES
    assert error["action"] in ERROR_ACTIONS
    assert isinstance(error["retryable"], bool)


def test_invalid_request_replaces_invalid_provider_misuse(
    client: TestClient, auth: dict[str, str]
) -> None:
    resp = client.get("/v1/agents", headers=auth, params={"cursor": "zzz"})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["action"] == "fix_request"
    assert error["retryable"] is False
    # invalid_provider remains for actual provider-field failures.
    resp = client.post(
        "/v1/agents",
        headers=auth,
        json={"prompt": {"text": "hi"}, "agent": {"provider": "bogus"}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_provider"


def test_error_status_by_code_matches_catalog() -> None:
    from control.api_v1.openapi import x_canonical

    assert x_canonical()["error_status_by_code"] == {
        code: spec.status for code, spec in sorted(ERROR_CATALOG.items())
    }

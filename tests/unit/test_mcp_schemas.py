"""MCP argument models: explicit selection rules, bounds, and error text that never echoes input."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sbx.mcp_server.errors import from_validation
from sbx.mcp_server.tools import BY_NAME, TOOLS, CreateTaskArgs, OutputTooLarge, bounded

BASE = {
    "prompt": "hello",
    "harness_id": "opencode",
    "executor_backend": "local",
    "inference_mode": "custom_api",
    "inference_connection_id": "conn_1",
    "idempotency_key": "create-key-0001",
}


def _create(**overrides: object) -> CreateTaskArgs:
    return CreateTaskArgs.model_validate({**BASE, **overrides})


def test_custom_api_needs_an_explicit_connection_and_no_slot() -> None:
    assert _create().inference_connection_id == "conn_1"
    with pytest.raises(ValidationError, match="no automatic choice"):
        _create(inference_connection_id=None)
    with pytest.raises(ValidationError, match="must not set machine_slot_id"):
        _create(machine_slot_id="slot_1")


def test_subscription_needs_a_slot_on_modal_and_no_api_key_connection() -> None:
    sub = {"inference_mode": "subscription", "inference_connection_id": None}
    ok = _create(**sub, machine_slot_id="slot_1", executor_backend="modal")
    assert ok.machine_slot_id == "slot_1"
    with pytest.raises(ValidationError, match="no fallback to an API key"):
        _create(**sub, executor_backend="modal")
    with pytest.raises(ValidationError, match="must not set inference_connection_id"):
        _create(inference_mode="subscription", machine_slot_id="slot_1", executor_backend="modal")
    with pytest.raises(ValidationError, match="run on executor_backend modal"):
        _create(**sub, machine_slot_id="slot_1")


def test_resident_needs_its_machine_and_other_backends_must_not_name_one() -> None:
    machine = _create(executor_backend="resident", resident_machine_id="rmach_1")
    assert machine.resident_machine_id == "rmach_1"
    with pytest.raises(ValidationError, match="needs resident_machine_id"):
        _create(executor_backend="resident")
    with pytest.raises(ValidationError, match="only valid with executor_backend resident"):
        _create(resident_machine_id="rmach_1")


def test_bounds_and_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _create(prompt="x" * 20001)
    with pytest.raises(ValidationError):
        _create(idempotency_key="short")
    with pytest.raises(ValidationError):
        _create(surprise="field")


def test_validation_errors_never_echo_the_submitted_values() -> None:
    secret = "sk-test-SECRET-VALUE-123456"
    with pytest.raises(ValidationError) as err:
        _create(inference_connection_id=None, prompt=secret, idempotency_key="short")
    result = from_validation(err.value)
    assert secret not in str(result.structured_content)
    assert result.is_error


def test_every_tool_schema_is_closed_and_described() -> None:
    assert len(TOOLS) == len(BY_NAME) == 11
    for spec in TOOLS:
        tool = spec.describe()
        assert tool.input_schema["additionalProperties"] is False, spec.name
        assert tool.output_schema["type"] == "object", spec.name
        assert tool.description, spec.name


def test_output_over_the_bound_is_refused_not_truncated() -> None:
    with pytest.raises(OutputTooLarge):
        bounded({"text": "x" * (64 * 1024 + 1)})
    assert bounded({"text": "ok"}) == {"text": "ok"}

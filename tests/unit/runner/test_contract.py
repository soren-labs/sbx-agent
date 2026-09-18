"""Deterministic tests for ``runtime.runner.contract`` (SOR-130).

Pure-Python: no subprocesses, no provider CLIs, no credentials — the
normalize/extract/validate/evaluate seams are exercised directly.
"""

from __future__ import annotations

import json

import pytest
from runtime.runner.contract import (
    ContractError,
    compile_schema,
    contract_instruction,
    evaluate_output,
    extract_json,
    load_contract_file,
    normalize_contract,
    schema_digest,
    validate,
    violation_summary,
)

SCHEMA = {
    "type": "object",
    "required": ["summary", "ok"],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "ok": {"type": "boolean"},
        "files": {"type": "array", "items": {"type": "string"}},
        "score": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "additionalProperties": False,
}
VALUE = {"summary": "created hello.txt", "ok": True, "files": ["hello.txt"]}


# ------------------------------------------------------------ normalize


def test_normalize_contract_defaults_and_digest() -> None:
    contract = normalize_contract({"schema": SCHEMA})
    assert contract["enforcement"] == "strict"
    assert contract["schema"] == SCHEMA
    assert contract["schema_digest"].startswith("sha256:")
    # Digest is canonical — key order does not matter.
    reordered = {"enforcement": "strict", "schema": dict(reversed(list(SCHEMA.items())))}
    assert schema_digest(reordered["schema"]) == contract["schema_digest"]


def test_normalize_contract_rejects_bad_shapes() -> None:
    for raw in (
        None,
        [],
        "x",
        {},
        {"schema": {}},
        {"schema": "type: object"},
        {"schema": SCHEMA, "enforcement": "loose"},
        {"schema": SCHEMA, "extra": 1},
    ):
        with pytest.raises(ContractError):
            normalize_contract(raw)


def test_compile_schema_rejects_unsupported_keywords() -> None:
    for schema in (
        {"$ref": "#/defs/x"},
        {"type": "object", "patternProperties": {"^x": {}}},
        {"if": {"type": "string"}, "then": {}},
        {"properties": {"a": {"$ref": "#/defs/a"}}},
    ):
        with pytest.raises(ContractError):
            compile_schema(schema)
    # ...but the error message names the offending keyword for diagnosis.
    with pytest.raises(ContractError, match=r"\$ref"):
        compile_schema({"$ref": "#/defs/x"})


def test_compile_schema_rejects_malformed_keywords() -> None:
    with pytest.raises(ContractError):
        compile_schema({"type": "frobnicate"})
    with pytest.raises(ContractError):
        compile_schema({"required": "field"})
    with pytest.raises(ContractError):
        compile_schema({"pattern": "("})
    with pytest.raises(ContractError):
        compile_schema({"minLength": -1})
    with pytest.raises(ContractError):
        compile_schema({"multipleOf": 0})
    with pytest.raises(ContractError):
        compile_schema({"allOf": []})


def test_load_contract_file(tmp_path) -> None:
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"schema": SCHEMA, "enforcement": "warn"}))
    contract = load_contract_file(path)
    assert contract["enforcement"] == "warn"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ContractError):
        load_contract_file(bad)


def test_contract_instruction_carries_schema() -> None:
    instruction = contract_instruction(SCHEMA)
    assert "[output-contract]" in instruction
    assert json.dumps({"type": "object"})[:10] in instruction or '"type":"object"' in instruction


# ------------------------------------------------------------ extract


def test_extract_json_raw() -> None:
    value, how = extract_json(json.dumps(VALUE))
    assert how == "raw"
    assert value == VALUE


def test_extract_json_fence() -> None:
    text = f"Here you go:\n```json\n{json.dumps(VALUE)}\n```\nDone."
    value, how = extract_json(text)
    assert how == "fence"
    assert value == VALUE


def test_extract_json_embedded() -> None:
    text = f"Result: {json.dumps(VALUE)} — hope that helps"
    value, how = extract_json(text)
    assert how == "embedded"
    assert value == VALUE


def test_extract_json_prefers_last_fence() -> None:
    text = '```\n{"a": 1}\n```\nthen\n```\n{"a": 2}\n```'
    value, how = extract_json(text)
    assert how == "fence"
    assert value == {"a": 2}


def test_extract_json_scalars_and_arrays() -> None:
    assert extract_json("[1, 2]") == ([1, 2], "raw")
    assert extract_json("42") == (42, "raw")
    assert extract_json('"text"') == ("text", "raw")
    assert extract_json("true") == (True, "raw")


def test_extract_json_none_for_prose() -> None:
    for text in (
        "",
        "Created hello.txt in the workspace.",
        "{}",
    ):
        value, how = extract_json(text)
        if text == "{}":
            assert (value, how) == ({}, "raw")
        else:
            assert (value, how) == (None, None)


# ------------------------------------------------------------ validate


def test_validate_ok() -> None:
    assert validate(VALUE, SCHEMA) == []


@pytest.mark.parametrize(
    ("instance", "schema", "code"),
    [
        ({"summary": "x"}, SCHEMA, "required"),
        ({"summary": "x", "ok": True, "extra": 1}, SCHEMA, "additionalProperties"),
        ({"summary": 1, "ok": True}, SCHEMA, "type"),
        ({"summary": "", "ok": True}, SCHEMA, "minLength"),
        ({"summary": "x", "ok": True, "score": 2}, SCHEMA, "maximum"),
        ("abc", {"type": "string", "pattern": "^a+$"}, "pattern"),
        ("x", {"enum": ["a", "b"]}, "enum"),
        ("x", {"const": "y"}, "const"),
        ([1, 2, 2], {"type": "array", "uniqueItems": True}, "uniqueItems"),
        ([1], {"type": "array", "minItems": 2}, "minItems"),
        ([1, 2, 3], {"type": "array", "items": {"type": "string"}}, "type"),
        ("b", {"type": "string", "allOf": [{"minLength": 2}]}, "allOf"),
        ("b", {"type": "string", "anyOf": [{"minLength": 2}, {"maxLength": 0}]}, "anyOf"),
        ("b", {"type": "string", "oneOf": [{"minLength": 1}, {"maxLength": 1}]}, "oneOf"),
        ("b", {"not": {"const": "b"}}, "not"),
        ({"a": 1}, {"dependentRequired": {"a": ["b"]}}, "dependentRequired"),
        ([1, 2], {"prefixItems": [{"type": "string"}]}, "type"),
        (3, {"type": "integer", "multipleOf": 2}, "multipleOf"),
        ([1, 2], {"contains": {"const": 3}}, "contains"),
        ({"x": 1}, {"propertyNames": {"pattern": "^y"}}, "pattern"),
        ([True, True], {"uniqueItems": True}, "uniqueItems"),
    ],
)
def test_validate_violations(instance, schema, code: str) -> None:
    compile_schema(schema)
    violations = validate(instance, schema)
    assert violations, f"expected violations for {instance!r}"
    assert violations[0]["code"] == code
    assert violations[0]["path"].startswith("$")
    assert violations[0]["message"]


def test_validate_violation_shape_is_machine_diagnosable() -> None:
    violations = validate({"summary": 3}, SCHEMA)
    assert {v["code"] for v in violations} == {"required", "type"}
    assert all({"path", "code", "message"} == set(v) for v in violations)
    by_path = {v["path"]: v["code"] for v in violations}
    assert by_path["$"] == "required"  # missing "ok" at the object root
    assert by_path["$.summary"] == "type"
    # missing required property names the property at the object path
    violations = validate({"ok": True}, SCHEMA)
    assert violations[0]["code"] == "required"
    assert "summary" in violations[0]["message"]


def test_validate_json_equality_semantics() -> None:
    # 1 and 1.0 are the same JSON number; bools are distinct.
    assert validate(1.0, {"const": 1}) == []
    assert validate(True, {"const": 1}) != []
    # Nested object equality is order-insensitive.
    assert validate({"a": 1, "b": [1, 2]}, {"const": {"b": [1, 2], "a": 1}}) == []


# ------------------------------------------------------------ evaluate


def test_evaluate_output_valid() -> None:
    verdict = evaluate_output(json.dumps(VALUE), SCHEMA)
    assert verdict["status"] == "valid"
    assert verdict["value"] == VALUE
    assert verdict["extraction"] == "raw"
    assert verdict["violations"] == []


def test_evaluate_output_invalid_schema() -> None:
    verdict = evaluate_output(json.dumps({"summary": "x"}), SCHEMA)
    assert verdict["status"] == "invalid"
    assert verdict["value"] == {"summary": "x"}
    assert verdict["violations"][0]["code"] == "required"


def test_evaluate_output_not_json() -> None:
    verdict = evaluate_output("Created hello.txt in the workspace.", SCHEMA)
    assert verdict["status"] == "invalid"
    assert verdict["value"] is None
    assert verdict["extraction"] is None
    assert verdict["violations"][0]["code"] == "not_json"


def test_violation_summary() -> None:
    verdict = evaluate_output("nope", SCHEMA)
    summary = violation_summary(verdict)
    assert "not_json" in summary

"""Structured output contract (SOR-130).

An optional JSON Schema contract attached to a run. The runner appends a
deterministic instruction to the provider prompt so the final agent message
carries one JSON value, then extracts and validates that value; the control
plane re-evaluates the recorded message authoritatively before persisting
the terminal run. This module is stdlib-only: it must import inside the
sandbox image (``runtime/`` only) and inside the control plane.

Shapes:

- Requested contract (``output_contract`` on create-agent/create-run):
  ``{"schema": {...}, "enforcement": "strict"|"warn"}`` — normalized with a
  pinned ``schema_digest``.
- Evaluation result (``turns/<n>.json.output_contract`` and the run view):
  ``{"enforcement", "schema_digest", "status", "extraction", "violations"}``
  where ``status`` is ``valid`` | ``invalid`` | ``skipped`` and each
  violation is ``{"path", "code", "message"}`` — machine-diagnosable.

Schema subset (fail-closed): ``type`` / ``enum`` / ``const`` /
``properties`` / ``required`` / ``additionalProperties`` / ``propertyNames``
/ ``minProperties`` / ``maxProperties`` / ``dependentRequired`` / ``items``
/ ``prefixItems`` / ``minItems`` / ``maxItems`` / ``uniqueItems`` /
``contains`` / ``minContains`` / ``maxContains`` / ``minLength`` /
``maxLength`` / ``pattern`` / ``minimum`` / ``maximum`` /
``exclusiveMinimum`` / ``exclusiveMaximum`` / ``multipleOf`` / ``allOf`` /
``anyOf`` / ``oneOf`` / ``not``. Annotation keywords (``$schema``, ``$id``,
``title``, ``description``, ``default``, ``format``, ...) are accepted and
ignored. Anything else — ``$ref``, ``if``/``then``/``else``,
``patternProperties``, ... — is rejected at compile time so a contract can
never silently validate more than it declares.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

ENFORCEMENTS: tuple[str, ...] = ("strict", "warn")

# Contract result statuses.
STATUS_VALID = "valid"
STATUS_INVALID = "invalid"
STATUS_SKIPPED = "skipped"  # turn never produced evaluable output
STATUS_PENDING = "pending"  # run still open

_MAX_VIOLATIONS = 25
_MESSAGE_LIMIT = 300

# Keywords with assertion semantics this validator implements.
_ASSERTION_KEYWORDS = frozenset(
    {
        "type",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "propertyNames",
        "minProperties",
        "maxProperties",
        "dependentRequired",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "uniqueItems",
        "contains",
        "minContains",
        "maxContains",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
    }
)

# Keywords accepted but ignored (annotation semantics in every draft).
_ANNOTATION_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$comment",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "readOnly",
        "writeOnly",
        "format",
        "$vocabulary",
    }
)

_INSTANCE_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")


class ContractError(ValueError):
    """Malformed or unsupported output contract / JSON Schema."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def schema_digest(schema: dict[str, Any]) -> str:
    """``sha256:<hex>`` over the canonical serialization — pins the schema."""
    return "sha256:" + hashlib.sha256(_canonical_json(schema).encode("utf-8")).hexdigest()


def normalize_contract(raw: Any) -> dict[str, Any]:
    """Validate + normalize a requested ``output_contract`` body.

    Returns ``{"schema": dict, "enforcement": str, "schema_digest": str}``;
    raises :class:`ContractError` on anything unusable so callers can refuse
    the request (400) instead of running a contract that cannot be enforced.
    """
    if not isinstance(raw, dict):
        raise ContractError("output_contract must be an object")
    unknown = set(raw) - {"schema", "enforcement"}
    if unknown:
        raise ContractError(f"output_contract has unknown fields: {sorted(unknown)}")
    schema = raw.get("schema")
    if not isinstance(schema, dict) or not schema:
        raise ContractError("output_contract.schema must be a non-empty JSON object")
    enforcement = raw.get("enforcement", "strict")
    if enforcement not in ENFORCEMENTS:
        raise ContractError(f"enforcement must be one of {list(ENFORCEMENTS)}")
    compile_schema(schema)
    return {
        "schema": schema,
        "enforcement": enforcement,
        "schema_digest": schema_digest(schema),
    }


def load_contract_file(path: str | Path) -> dict[str, Any]:
    """Read a control-plane-written contract file; normalize + compile it."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"contract file is not valid JSON: {exc}") from exc
    return normalize_contract(raw)


def compile_schema(schema: Any, *, _path: str = "$") -> None:
    """Fail-closed schema check: only the implemented assertion subset.

    Raises :class:`ContractError` naming the first unsupported or malformed
    construct so request-time validation can refuse it.
    """
    if isinstance(schema, bool):
        # JSON Schema allows boolean schemas; trivially supported.
        return
    if not isinstance(schema, dict):
        raise ContractError(f"schema at {_path} must be an object")
    for keyword in schema:
        if keyword in _ASSERTION_KEYWORDS or keyword in _ANNOTATION_KEYWORDS:
            continue
        raise ContractError(f"unsupported schema keyword {keyword!r} at {_path}")

    _check_type_decl(schema.get("type"), _path)
    for keyword in ("enum",):
        if keyword in schema and not isinstance(schema[keyword], list):
            raise ContractError(f"{keyword} at {_path} must be an array")
    for keyword in ("required",):
        if keyword in schema:
            req = schema[keyword]
            if (
                not isinstance(req, list)
                or any(not isinstance(r, str) for r in req)
                or len(set(req)) != len(req)
            ):
                raise ContractError(f"required at {_path} must be a unique string array")
    if "dependentRequired" in schema:
        dep = schema["dependentRequired"]
        if not isinstance(dep, dict) or any(
            not isinstance(v, list) or any(not isinstance(x, str) for x in v) for v in dep.values()
        ):
            raise ContractError(f"dependentRequired at {_path} must map names to string arrays")
    for keyword in (
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
        "minContains",
        "maxContains",
    ):
        if keyword in schema and (
            isinstance(schema[keyword], bool)
            or not isinstance(schema[keyword], int)
            or schema[keyword] < 0
        ):
            raise ContractError(f"{keyword} at {_path} must be a non-negative integer")
    for keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if keyword in schema and (
            isinstance(schema[keyword], bool) or not isinstance(schema[keyword], (int, float))
        ):
            raise ContractError(f"{keyword} at {_path} must be a number")
    if "multipleOf" in schema and (
        isinstance(schema["multipleOf"], bool)
        or not isinstance(schema["multipleOf"], (int, float))
        or schema["multipleOf"] <= 0
    ):
        raise ContractError(f"multipleOf at {_path} must be a positive number")
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise ContractError(f"pattern at {_path} must be a string")
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            raise ContractError(f"pattern at {_path} is not a valid regex: {exc}") from exc
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        raise ContractError(f"uniqueItems at {_path} must be a boolean")

    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict):
            raise ContractError(f"properties at {_path} must be an object")
        for name, subschema in props.items():
            compile_schema(subschema, _path=f"{_path}.properties[{name!r}]")
    if "additionalProperties" in schema:
        compile_schema(schema["additionalProperties"], _path=f"{_path}.additionalProperties")
    if "propertyNames" in schema:
        compile_schema(schema["propertyNames"], _path=f"{_path}.propertyNames")
    if "items" in schema:
        compile_schema(schema["items"], _path=f"{_path}.items")
    if "prefixItems" in schema:
        prefix = schema["prefixItems"]
        if not isinstance(prefix, list):
            raise ContractError(f"prefixItems at {_path} must be an array")
        for index, subschema in enumerate(prefix):
            compile_schema(subschema, _path=f"{_path}.prefixItems[{index}]")
    if "contains" in schema:
        compile_schema(schema["contains"], _path=f"{_path}.contains")
    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword in schema:
            subs = schema[keyword]
            if not isinstance(subs, list) or not subs:
                raise ContractError(f"{keyword} at {_path} must be a non-empty array")
            for index, subschema in enumerate(subs):
                compile_schema(subschema, _path=f"{_path}.{keyword}[{index}]")
    if "not" in schema:
        compile_schema(schema["not"], _path=f"{_path}.not")


def _check_type_decl(decl: Any, path: str) -> None:
    if decl is None:
        return
    names = decl if isinstance(decl, list) else [decl]
    if not names or any(name not in _INSTANCE_TYPES for name in names):
        raise ContractError(f"type at {path} must be one of {list(_INSTANCE_TYPES)}")


def contract_instruction(schema: dict[str, Any]) -> str:
    """Prompt suffix steering any provider CLI to emit contract JSON."""
    return (
        "\n\n[output-contract] Your final message must be exactly one JSON"
        " value that validates against this JSON Schema — no markdown fences,"
        " no commentary before or after it:\n" + _canonical_json(schema)
    )


# ------------------------------------------------------------- extraction


def extract_json(text: str) -> tuple[Any, str | None]:
    """Deterministically pull one JSON value out of an agent message.

    Returns ``(value, how)`` where ``how`` is ``"raw"`` (the whole message),
    ``"fence"`` (a ``` fenced block), or ``"embedded"`` (a balanced
    ``{...}``/``[...]`` span); ``(None, None)`` when nothing parses. Later
    candidates win — a model's final answer sits at the end of its message.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None, None
    try:
        return json.loads(stripped), "raw"
    except json.JSONDecodeError:
        pass

    # Fenced code blocks, last-to-first.
    fences = list(re.finditer(r"```[^\n]*\n(.*?)```", stripped, re.S))
    for match in reversed(fences):
        candidate = match.group(1).strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate), "fence"
        except json.JSONDecodeError:
            continue

    # Balanced spans: raw_decode at each '{'/'['. The candidate whose span
    # ends latest wins — the model's final answer sits at the end of its
    # message — with the longest span breaking ties so an outer object
    # beats the arrays/objects nested inside it.
    decoder = json.JSONDecoder()
    best: tuple[int, int, Any] | None = None  # (end, -start, value)
    for index, char in enumerate(stripped):
        if char not in "{[":
            continue
        try:
            value, end = decoder.raw_decode(stripped, index)
        except json.JSONDecodeError:
            continue
        if best is None or (end, -index) > (best[0], best[1]):
            best = (end, -index, value)
    if best is not None:
        return best[2], "embedded"
    return None, None


# ------------------------------------------------------------- validation


def validate(instance: Any, schema: Any) -> list[dict[str, str]]:
    """Validate ``instance`` against the compiled-subset schema.

    Returns a bounded list of ``{"path", "code", "message"}`` violations;
    empty means valid. ``schema`` must already pass :func:`compile_schema`.
    """
    violations: list[dict[str, str]] = []
    _validate(instance, schema, "$", violations)
    return violations[:_MAX_VIOLATIONS]


def _viol(violations: list[dict[str, str]], path: str, code: str, message: str) -> None:
    if len(violations) < _MAX_VIOLATIONS:
        violations.append({"path": path, "code": code, "message": message[:_MESSAGE_LIMIT]})


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _type_matches(value: Any, name: str) -> bool:
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    return False


def _json_equal(left: Any, right: Any) -> bool:
    """JSON-equality: 1 == 1.0, dict/list order-insensitive per spec."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_json_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return left == right


def _validate(instance: Any, schema: Any, path: str, violations: list[dict[str, str]]) -> None:
    if isinstance(schema, bool):
        if not schema:
            _viol(violations, path, "false_schema", "value rejected by false schema")
        return

    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_matches(instance, name) for name in names):
            _viol(
                violations,
                path,
                "type",
                f"expected type {'/'.join(names)}, got {_type_name(instance)}",
            )
            return  # downstream keywords would cascade noise

    if "const" in schema and not _json_equal(instance, schema["const"]):
        _viol(violations, path, "const", f"value must equal {_canonical_json(schema['const'])}")
    if "enum" in schema and not any(_json_equal(instance, option) for option in schema["enum"]):
        _viol(violations, path, "enum", "value is not one of the declared enum values")

    if isinstance(instance, dict):
        _validate_object(instance, schema, path, violations)
    elif isinstance(instance, list):
        _validate_array(instance, schema, path, violations)
    elif isinstance(instance, str):
        _validate_string(instance, schema, path, violations)
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        _validate_number(instance, schema, path, violations)

    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword not in schema:
            continue
        subs = schema[keyword]
        counts = sum(1 for sub in subs if not _collect(lambda v: _validate(instance, sub, path, v)))
        if keyword == "allOf" and counts != len(subs):
            _viol(violations, path, "allOf", "value fails an allOf subschema")
        if keyword == "anyOf" and counts == 0:
            _viol(violations, path, "anyOf", "value matches no anyOf subschema")
        if keyword == "oneOf" and counts != 1:
            _viol(
                violations,
                path,
                "oneOf",
                f"value matches {counts} oneOf subschemas (exactly one required)",
            )
    if "not" in schema:
        if not _collect(lambda v: _validate(instance, schema["not"], path, v)):
            _viol(violations, path, "not", "value matches the forbidden not subschema")


def _collect(check: Any) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    check(violations)
    return violations


def _validate_object(
    instance: dict[str, Any], schema: dict[str, Any], path: str, violations: list[dict[str, str]]
) -> None:
    for name in schema.get("required") or ():
        if name not in instance:
            _viol(violations, path, "required", f"missing required property {name!r}")
    props = schema.get("properties") or {}
    for name, subschema in props.items():
        if name in instance:
            _validate(instance[name], subschema, f"{path}.{name}", violations)
    additional = schema.get("additionalProperties", True)
    extras = [name for name in instance if name not in props]
    if additional is False and extras:
        _viol(
            violations,
            path,
            "additionalProperties",
            f"unexpected properties: {extras[:10]}",
        )
    elif isinstance(additional, dict):
        for name in extras:
            _validate(instance[name], additional, f"{path}.{name}", violations)
    if "propertyNames" in schema:
        for name in instance:
            _validate(name, schema["propertyNames"], f"{path}.{name}", violations)
    if "minProperties" in schema and len(instance) < schema["minProperties"]:
        _viol(violations, path, "minProperties", "object has too few properties")
    if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
        _viol(violations, path, "maxProperties", "object has too many properties")
    for name, deps in (schema.get("dependentRequired") or {}).items():
        if name in instance:
            for dep in deps:
                if dep not in instance:
                    _viol(
                        violations,
                        path,
                        "dependentRequired",
                        f"property {name!r} requires {dep!r}",
                    )


def _validate_array(
    instance: list[Any], schema: dict[str, Any], path: str, violations: list[dict[str, str]]
) -> None:
    prefix = schema.get("prefixItems") or []
    for index, subschema in enumerate(prefix[: len(instance)]):
        _validate(instance[index], subschema, f"{path}[{index}]", violations)
    if "items" in schema:
        for index in range(len(prefix), len(instance)):
            _validate(instance[index], schema["items"], f"{path}[{index}]", violations)
    if "minItems" in schema and len(instance) < schema["minItems"]:
        _viol(violations, path, "minItems", "array has too few items")
    if "maxItems" in schema and len(instance) > schema["maxItems"]:
        _viol(violations, path, "maxItems", "array has too many items")
    if schema.get("uniqueItems"):
        for i in range(len(instance)):
            for j in range(i + 1, len(instance)):
                if _json_equal(instance[i], instance[j]):
                    _viol(violations, path, "uniqueItems", "array items are not unique")
                    break
            else:
                continue
            break
    if "contains" in schema:
        matched = sum(
            1
            for item in instance
            if not _collect(lambda v: _validate(item, schema["contains"], path, v))
        )
        minimum = schema.get("minContains", 1)
        if matched < minimum:
            _viol(
                violations,
                path,
                "contains",
                f"array has {matched} matching items (minContains={minimum})",
            )
        maximum = schema.get("maxContains")
        if maximum is not None and matched > maximum:
            _viol(
                violations,
                path,
                "contains",
                f"array has {matched} matching items (maxContains={maximum})",
            )


def _validate_string(
    instance: str, schema: dict[str, Any], path: str, violations: list[dict[str, str]]
) -> None:
    if "minLength" in schema and len(instance) < schema["minLength"]:
        _viol(violations, path, "minLength", "string is shorter than minLength")
    if "maxLength" in schema and len(instance) > schema["maxLength"]:
        _viol(violations, path, "maxLength", "string is longer than maxLength")
    if "pattern" in schema and not re.search(schema["pattern"], instance):
        _viol(violations, path, "pattern", "string does not match pattern")


def _validate_number(
    instance: int | float, schema: dict[str, Any], path: str, violations: list[dict[str, str]]
) -> None:
    if isinstance(instance, float) and (math.isnan(instance) or math.isinf(instance)):
        _viol(violations, path, "number", "non-finite number")
        return
    if "minimum" in schema and instance < schema["minimum"]:
        _viol(violations, path, "minimum", f"value is below minimum {schema['minimum']}")
    if "maximum" in schema and instance > schema["maximum"]:
        _viol(violations, path, "maximum", f"value is above maximum {schema['maximum']}")
    if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
        _viol(
            violations,
            path,
            "exclusiveMinimum",
            f"value is not above exclusiveMinimum {schema['exclusiveMinimum']}",
        )
    if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
        _viol(
            violations,
            path,
            "exclusiveMaximum",
            f"value is not below exclusiveMaximum {schema['exclusiveMaximum']}",
        )
    if "multipleOf" in schema:
        quotient = instance / schema["multipleOf"]
        if not math.isclose(quotient, round(quotient), rel_tol=0, abs_tol=1e-9):
            _viol(
                violations,
                path,
                "multipleOf",
                f"value is not a multiple of {schema['multipleOf']}",
            )


# ------------------------------------------------------------- evaluation


def evaluate_output(text: str | None, schema: dict[str, Any]) -> dict[str, Any]:
    """Extract + validate the agent message against ``schema``.

    Returns ``{"status", "value", "extraction", "violations"}`` — the shared
    verdict used by both the in-sandbox runner and the control plane, so a
    contract can never pass on one side and fail on the other.
    """
    value, how = extract_json(text or "")
    if how is None:
        return {
            "status": STATUS_INVALID,
            "value": None,
            "extraction": None,
            "violations": [
                {
                    "path": "$",
                    "code": "not_json",
                    "message": "agent message contains no parseable JSON value",
                }
            ],
        }
    violations = validate(value, schema)
    return {
        "status": STATUS_VALID if not violations else STATUS_INVALID,
        "value": value,
        "extraction": how,
        "violations": violations,
    }


def violation_summary(evaluation: dict[str, Any]) -> str:
    """One-line machine-friendly digest of an evaluation for ``run.error``."""
    violations = evaluation.get("violations") or []
    if not violations:
        return "output contract not evaluated"
    first = violations[0]
    rest = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    return f"{first.get('code')}: {first.get('message')}{rest}"

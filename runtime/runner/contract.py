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
``maxLength`` / ``minimum`` / ``maximum`` / ``exclusiveMinimum`` /
``exclusiveMaximum`` / ``multipleOf`` / ``allOf`` / ``anyOf`` / ``oneOf`` /
``not``. Annotation keywords (``$schema``, ``$id``, ``title``,
``description``, ``default``, ``format``, ...) are accepted and ignored.
Anything else — ``$ref``, ``if``/``then``/``else``, ``patternProperties``,
... — is rejected at compile time so a contract can never silently validate
more than it declares. ``pattern`` is deliberately outside the subset:
stdlib ``re`` offers no execution budget and no linear-time engine, so a
client-controlled regex could stall the evaluator on agent output (ReDoS)
— string contracts express bounds via ``minLength`` / ``maxLength`` /
``enum`` / ``const`` instead.
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

# Evaluation budgets. The verdict seam judges untrusted input — a client-
# supplied schema at request time and a sandbox-written agent message at
# persist time — so every recursive/scanning step is bounded. Exceeding a
# bound is a verdict (``max_depth`` / ``instance_too_large`` /
# ``evaluation_budget`` / failed extraction), never an exception and never
# a stall.
_MAX_SCHEMA_DEPTH = 100  # compile bound on schema-keyword nesting
_MAX_SCHEMA_BYTES = 64 << 10  # canonical-size bound on the client schema
_MAX_EVAL_DEPTH = 200  # instance container nesting the validator will judge
_MAX_EVAL_NODES = 50_000  # total JSON nodes in the instance to judge
_MAX_EVAL_STEPS = 200_000  # node-visits/atom-checks per evaluate_output
_EMBED_SCAN_BUDGET = 4 << 20  # total chars examined across embedded candidates
_EMBED_MAX_ATTEMPTS = 4096  # raw_decode attempts per message

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
    if _json_depth(schema) > _MAX_EVAL_DEPTH:
        # The schema is itself serialized (digest, dispatch file, run view)
        # — bound its total nesting so it can never trip a serializer.
        raise ContractError(f"output_contract.schema nests deeper than {_MAX_EVAL_DEPTH}")
    enforcement = raw.get("enforcement", "strict")
    if enforcement not in ENFORCEMENTS:
        raise ContractError(f"enforcement must be one of {list(ENFORCEMENTS)}")
    try:
        compile_schema(schema)
        canonical = _canonical_json(schema)
        if len(canonical) > _MAX_SCHEMA_BYTES:
            # An unbounded schema buys the client unbounded per-run
            # evaluation fanout — refuse it at request time like any
            # other unenforceable contract.
            raise ContractError(
                f"output_contract.schema serializes past the {_MAX_SCHEMA_BYTES}-byte bound"
            )
        digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except ContractError:
        raise
    except Exception as exc:
        # Adversarial payload shapes (e.g. nesting that only trips the JSON
        # serializer) must surface as a refused contract, never an
        # uncaught 500.
        raise ContractError(
            f"output_contract.schema cannot be enforced: {type(exc).__name__}"
        ) from exc
    return {
        "schema": schema,
        "enforcement": enforcement,
        "schema_digest": digest,
    }


def load_contract_file(path: str | Path) -> dict[str, Any]:
    """Read a control-plane-written contract file; normalize + compile it."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        raw = _loads(text)
    except Exception as exc:
        raise ContractError(f"contract file is not valid JSON: {exc}") from exc
    return normalize_contract(raw)


def compile_schema(schema: Any, *, _path: str = "$", _depth: int = 0) -> None:
    """Fail-closed schema check: only the implemented assertion subset.

    Raises :class:`ContractError` naming the first unsupported or malformed
    construct so request-time validation can refuse it. Keyword nesting is
    bounded by ``_MAX_SCHEMA_DEPTH`` — a schema too deep to evaluate is a
    malformed contract, not a crash.
    """
    if isinstance(schema, bool):
        # JSON Schema allows boolean schemas; trivially supported.
        return
    if not isinstance(schema, dict):
        raise ContractError(f"schema at {_path} must be an object")
    if _depth > _MAX_SCHEMA_DEPTH:
        raise ContractError(f"schema at {_path} nests deeper than {_MAX_SCHEMA_DEPTH}")
    for keyword in schema:
        if keyword in _ASSERTION_KEYWORDS or keyword in _ANNOTATION_KEYWORDS:
            continue
        raise ContractError(f"unsupported schema keyword {keyword!r} at {_path}")

    _check_type_decl(schema.get("type"), _path)
    # Leaf values the compiler doesn't recurse into — annotations are
    # ignored but still serialized for the digest/dispatch, so their
    # nesting is bounded like const/enum.
    for keyword in _ANNOTATION_KEYWORDS:
        if keyword in schema and _json_depth(schema[keyword]) > _MAX_EVAL_DEPTH:
            raise ContractError(f"{keyword} at {_path} nests deeper than {_MAX_EVAL_DEPTH}")
    if "enum" in schema:
        if not isinstance(schema["enum"], list):
            raise ContractError(f"enum at {_path} must be an array")
        if any(_json_depth(option) > _MAX_EVAL_DEPTH for option in schema["enum"]):
            raise ContractError(f"enum at {_path} nests deeper than {_MAX_EVAL_DEPTH}")
    if "const" in schema and _json_depth(schema["const"]) > _MAX_EVAL_DEPTH:
        raise ContractError(f"const at {_path} nests deeper than {_MAX_EVAL_DEPTH}")
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
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        raise ContractError(f"uniqueItems at {_path} must be a boolean")

    depth = _depth + 1
    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict):
            raise ContractError(f"properties at {_path} must be an object")
        for name, subschema in props.items():
            compile_schema(subschema, _path=f"{_path}.properties[{name!r}]", _depth=depth)
    if "additionalProperties" in schema:
        compile_schema(
            schema["additionalProperties"], _path=f"{_path}.additionalProperties", _depth=depth
        )
    if "propertyNames" in schema:
        compile_schema(schema["propertyNames"], _path=f"{_path}.propertyNames", _depth=depth)
    if "items" in schema:
        compile_schema(schema["items"], _path=f"{_path}.items", _depth=depth)
    if "prefixItems" in schema:
        prefix = schema["prefixItems"]
        if not isinstance(prefix, list):
            raise ContractError(f"prefixItems at {_path} must be an array")
        for index, subschema in enumerate(prefix):
            compile_schema(subschema, _path=f"{_path}.prefixItems[{index}]", _depth=depth)
    if "contains" in schema:
        compile_schema(schema["contains"], _path=f"{_path}.contains", _depth=depth)
    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword in schema:
            subs = schema[keyword]
            if not isinstance(subs, list) or not subs:
                raise ContractError(f"{keyword} at {_path} must be a non-empty array")
            for index, subschema in enumerate(subs):
                compile_schema(subschema, _path=f"{_path}.{keyword}[{index}]", _depth=depth)
    if "not" in schema:
        compile_schema(schema["not"], _path=f"{_path}.not", _depth=depth)


def _check_type_decl(decl: Any, path: str) -> None:
    if decl is None:
        return
    names = decl if isinstance(decl, list) else [decl]
    if not names or any(name not in _INSTANCE_TYPES for name in names):
        raise ContractError(f"type at {path} must be one of {list(_INSTANCE_TYPES)}")


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name!r}")


def _loads(text: str) -> Any:
    """``json.loads`` restricted to spec-legal JSON — ``NaN``/``Infinity``
    are rejected so non-standard values cannot reach ``structured_output``."""
    return json.loads(text, parse_constant=_reject_constant)


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

    The embedded scan is bounded (``_EMBED_MAX_ATTEMPTS`` candidate starts,
    examined back-to-front so the final answer is reached first, under a
    shared ``_EMBED_SCAN_BUDGET`` that debits the span each attempt actually
    scanned): an unparseable ``{``/``[``-heavy message cannot turn
    extraction quadratic, and a candidate beyond the budget is treated as
    absent — a miss is a verdict, never a crash.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None, None
    try:
        return _loads(stripped), "raw"
    except (ValueError, RecursionError):
        pass

    # Fenced code blocks, last-to-first.
    fences = list(re.finditer(r"```[^\n]*\n(.*?)```", stripped, re.S))
    for match in reversed(fences):
        candidate = match.group(1).strip()
        if not candidate:
            continue
        try:
            return _loads(candidate), "fence"
        except (ValueError, RecursionError):
            continue

    # Balanced spans: raw_decode at each '{'/'['. The candidate whose span
    # ends latest wins — the model's final answer sits at the end of its
    # message — with the longest span breaking ties so an outer object
    # beats the arrays/objects nested inside it.
    decoder = json.JSONDecoder(parse_constant=_reject_constant)
    best: tuple[int, int, Any] | None = None  # (end, -start, value)
    attempts = 0
    budget = _EMBED_SCAN_BUDGET
    index = len(stripped) - 1
    while index >= 0 and attempts < _EMBED_MAX_ATTEMPTS and budget > 0:
        char = stripped[index]
        index -= 1
        if char not in "{[":
            continue
        attempts += 1
        start = index + 1
        try:
            value, end = decoder.raw_decode(stripped, start)
        except json.JSONDecodeError as exc:
            budget -= max(exc.pos - start, 1)  # span actually scanned
            continue
        except ValueError:
            # A rejected NaN/Infinity constant — a failed candidate, not a
            # crash. Debit the whole remaining tail: repeats are unlikely
            # to differ.
            budget -= len(stripped) - start
            continue
        except RecursionError:
            # Pathological nesting — the scan is already deep; stop here.
            break
        budget -= end - start
        if best is None or (end, -start) > (best[0], best[1]):
            best = (end, -start, value)
    if best is not None:
        return best[2], "embedded"
    return None, None


# ------------------------------------------------------------- validation


def _json_depth(value: Any) -> int:
    """Container-nesting depth of a decoded JSON value — iterative, so the
    measurement itself is immune to the recursion it bounds."""
    depth = 0
    stack = [(value, 0)]
    while stack:
        node, level = stack.pop()
        if isinstance(node, (dict, list)):
            if level > depth:
                depth = level
            children = node.values() if isinstance(node, dict) else node
            stack.extend((child, level + 1) for child in children)
    return depth


class _EvalBudgetExceeded(Exception):
    """Internal: the per-evaluation work budget ran out."""


class _Budget:
    """Deterministic work budget for one validation.

    Every recursive step and every loop iteration driven by untrusted input
    (instance members, schema-driven enumeration, equality walks) debits
    the counter; exhaustion is a verdict, never an exception escaping.
    """

    __slots__ = ("remaining",)

    def __init__(self, steps: int) -> None:
        self.remaining = steps

    def tick(self, n: int = 1) -> None:
        self.remaining -= n
        if self.remaining < 0:
            raise _EvalBudgetExceeded


def _eval_limit_breach(value: Any) -> str | None:
    """One iterative walk over the instance; returns the violated bound's
    code — ``max_depth`` or ``instance_too_large`` — or ``None``.

    Early-exits past the bound, so measuring a hostile instance is itself
    bounded work.
    """
    depth = 0
    nodes = 0
    stack = [(value, 0)]
    while stack:
        node, level = stack.pop()
        nodes += 1
        if nodes > _MAX_EVAL_NODES:
            return "instance_too_large"
        if isinstance(node, (dict, list)):
            if level > depth:
                depth = level
                if depth > _MAX_EVAL_DEPTH:
                    return "max_depth"
            children = node.values() if isinstance(node, dict) else node
            stack.extend((child, level + 1) for child in children)
    return None


_LIMIT_BREACH_MESSAGES = {
    "max_depth": f"instance nests deeper than the evaluation bound {_MAX_EVAL_DEPTH}",
    "instance_too_large": f"instance has more than {_MAX_EVAL_NODES} nodes",
}


def validate(instance: Any, schema: Any) -> list[dict[str, str]]:
    """Validate ``instance`` against the compiled-subset schema.

    Returns a bounded list of ``{"path", "code", "message"}`` violations;
    empty means valid. ``schema`` must already pass :func:`compile_schema`.
    Total: an instance past ``_MAX_EVAL_DEPTH``/``_MAX_EVAL_NODES`` reports
    ``max_depth``/``instance_too_large``, a schema × instance fanout past
    ``_MAX_EVAL_STEPS`` reports ``evaluation_budget``, and any other failure
    inside the check reports ``evaluation_error`` — pathological input is a
    verdict, never an exception or a stall.
    """
    violations: list[dict[str, str]] = []
    try:
        breach = _eval_limit_breach(instance)
        if breach is not None:
            _viol(violations, "$", breach, _LIMIT_BREACH_MESSAGES[breach])
        else:
            _validate(instance, schema, "$", violations, _Budget(_MAX_EVAL_STEPS))
    except _EvalBudgetExceeded:
        # Bounded-work verdict: keep the diagnoses gathered so far and pin
        # the breach itself as the last violation — a contract too
        # expensive to judge is a diagnosable invalid, never a stall.
        violations[:] = [
            *violations[: _MAX_VIOLATIONS - 1],
            {
                "path": "$",
                "code": "evaluation_budget",
                "message": f"evaluation exceeded the work bound {_MAX_EVAL_STEPS} steps",
            },
        ]
    except Exception as exc:
        # A schema that never saw compile_schema (a tampered ledger record)
        # can still carry a deep const or malformed keyword shape — fail
        # closed with a diagnosable violation rather than crashing the
        # enforcement seam.
        _viol(violations, "$", "evaluation_error", f"validation failed: {type(exc).__name__}")
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


def _json_equal(left: Any, right: Any, budget: _Budget) -> bool:
    """JSON-equality: 1 == 1.0, dict/list order-insensitive per spec."""
    budget.tick()
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[k], right[k], budget) for k in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(a, b, budget) for a, b in zip(left, right, strict=True)
        )
    return left == right


def _eq_key(value: Any, budget: _Budget) -> Any:
    """Hashable canonical form honoring JSON equality (``1 == 1.0``, dict
    order-insensitive) — lets ``uniqueItems`` dedupe in O(n) instead of
    pairwise ``_json_equal``."""
    budget.tick()
    if isinstance(value, bool):
        return (0, value)
    if value is None:
        return (1,)
    if isinstance(value, (int, float)):
        # Integral floats fold to int so 1 and 1.0 share a key while
        # arbitrary-precision ints stay exact.
        return (2, int(value) if isinstance(value, float) and value.is_integer() else value)
    if isinstance(value, str):
        return (3, value)
    if isinstance(value, list):
        return (4, tuple(_eq_key(item, budget) for item in value))
    if isinstance(value, dict):
        return (5, frozenset((key, _eq_key(item, budget)) for key, item in value.items()))
    return (6, repr(value))


def _validate(
    instance: Any,
    schema: Any,
    path: str,
    violations: list[dict[str, str]],
    budget: _Budget,
) -> None:
    # One step per visited (instance, schema) pair plus one per direct
    # container member — pre-pays every per-visit linear pass below so the
    # total work is bounded by _MAX_EVAL_STEPS whatever the fanout.
    budget.tick(1 + len(instance) if isinstance(instance, (dict, list)) else 1)
    if isinstance(schema, bool):
        if not schema:
            _viol(violations, path, "false_schema", "value rejected by false schema")
        return
    if not isinstance(schema, dict):
        _viol(violations, path, "evaluation_error", "subschema is not an object")
        return
    unknown = set(schema) - _ASSERTION_KEYWORDS - _ANNOTATION_KEYWORDS
    if unknown:
        # A schema that never saw compile_schema (a tampered ledger record)
        # carrying keywords outside the enforced subset must fail closed —
        # silently ignoring an assertion keyword could pass output the
        # contract meant to reject.
        _viol(
            violations,
            path,
            "evaluation_error",
            f"unsupported schema keyword {sorted(unknown)[0]!r} reached evaluation",
        )
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

    if "const" in schema and not _json_equal(instance, schema["const"], budget):
        _viol(violations, path, "const", f"value must equal {_canonical_json(schema['const'])}")
    if "enum" in schema and not any(
        _json_equal(instance, option, budget) for option in schema["enum"]
    ):
        _viol(violations, path, "enum", "value is not one of the declared enum values")

    if isinstance(instance, dict):
        _validate_object(instance, schema, path, violations, budget)
    elif isinstance(instance, list):
        _validate_array(instance, schema, path, violations, budget)
    elif isinstance(instance, str):
        _validate_string(instance, schema, path, violations)
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        _validate_number(instance, schema, path, violations)

    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword not in schema:
            continue
        subs = schema[keyword]
        counts = sum(
            1 for sub in subs if not _collect(lambda v: _validate(instance, sub, path, v, budget))
        )
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
        if not _collect(lambda v: _validate(instance, schema["not"], path, v, budget)):
            _viol(violations, path, "not", "value matches the forbidden not subschema")


def _collect(check: Any) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    check(violations)
    return violations


def _validate_object(
    instance: dict[str, Any],
    schema: dict[str, Any],
    path: str,
    violations: list[dict[str, str]],
    budget: _Budget,
) -> None:
    for name in schema.get("required") or ():
        budget.tick()
        if name not in instance:
            _viol(violations, path, "required", f"missing required property {name!r}")
    props = schema.get("properties") or {}
    for name in instance:
        subschema = props.get(name)
        if subschema is not None:
            _validate(instance[name], subschema, f"{path}.{name}", violations, budget)
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
            _validate(instance[name], additional, f"{path}.{name}", violations, budget)
    if "propertyNames" in schema:
        for name in instance:
            _validate(name, schema["propertyNames"], f"{path}.{name}", violations, budget)
    if "minProperties" in schema and len(instance) < schema["minProperties"]:
        _viol(violations, path, "minProperties", "object has too few properties")
    if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
        _viol(violations, path, "maxProperties", "object has too many properties")
    for name, deps in (schema.get("dependentRequired") or {}).items():
        budget.tick()
        if name in instance:
            for dep in deps:
                budget.tick()
                if dep not in instance:
                    _viol(
                        violations,
                        path,
                        "dependentRequired",
                        f"property {name!r} requires {dep!r}",
                    )


def _validate_array(
    instance: list[Any],
    schema: dict[str, Any],
    path: str,
    violations: list[dict[str, str]],
    budget: _Budget,
) -> None:
    prefix = schema.get("prefixItems") or []
    for index, subschema in enumerate(prefix[: len(instance)]):
        _validate(instance[index], subschema, f"{path}[{index}]", violations, budget)
    if "items" in schema:
        for index in range(len(prefix), len(instance)):
            _validate(instance[index], schema["items"], f"{path}[{index}]", violations, budget)
    if "minItems" in schema and len(instance) < schema["minItems"]:
        _viol(violations, path, "minItems", "array has too few items")
    if "maxItems" in schema and len(instance) > schema["maxItems"]:
        _viol(violations, path, "maxItems", "array has too many items")
    if schema.get("uniqueItems"):
        seen: set[Any] = set()
        for item in instance:
            key = _eq_key(item, budget)
            if key in seen:
                _viol(violations, path, "uniqueItems", "array items are not unique")
                break
            seen.add(key)
    if "contains" in schema:
        matched = sum(
            1
            for item in instance
            if not _collect(lambda v: _validate(item, schema["contains"], path, v, budget))
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


def _invalid_verdict(code: str, message: str) -> dict[str, Any]:
    return {
        "status": STATUS_INVALID,
        "value": None,
        "extraction": None,
        "violations": [{"path": "$", "code": code, "message": message[:_MESSAGE_LIMIT]}],
    }


def evaluate_output(text: str | None, schema: Any) -> dict[str, Any]:
    """Extract + validate the agent message against ``schema``.

    Returns ``{"status", "value", "extraction", "violations"}`` — the shared
    verdict used by both the in-sandbox runner and the control plane, so a
    contract can never pass on one side and fail on the other.

    Total: this seam judges untrusted output, so it never raises. A missing
    or unusable schema, unparseable/pathological output, an input past the
    evaluation bounds, or an unexpected failure inside evaluation all yield
    an ``invalid`` verdict with a machine-diagnosable violation
    (``not_json`` / ``max_depth`` / ``instance_too_large`` /
    ``evaluation_budget`` / ``evaluation_error``) — strict enforcement then
    fails closed instead of wedging or silently succeeding.
    """
    try:
        if not isinstance(schema, dict) or not schema:
            return _invalid_verdict(
                "evaluation_error", "contract schema is missing or not an object"
            )
        value, how = extract_json(text or "")
        if how is None:
            return _invalid_verdict("not_json", "agent message contains no parseable JSON value")
        violations = validate(value, schema)
        status = STATUS_VALID if not violations else STATUS_INVALID
        return {
            "status": status,
            # An extracted-but-invalid value is kept for diagnosis — except
            # one breaching the eval bounds (max_depth / instance_too_large):
            # it is never safe to hand downstream serializers, and the raw
            # message remains the evidence of record.
            "value": None if violations and _eval_limit_breach(value) else value,
            "extraction": how,
            "violations": violations,
        }
    except Exception as exc:
        return _invalid_verdict(
            "evaluation_error", f"evaluation failed: {type(exc).__name__}: {exc}"
        )


def violation_summary(evaluation: dict[str, Any]) -> str:
    """One-line machine-friendly digest of an evaluation for ``run.error``."""
    violations = evaluation.get("violations") or []
    if not violations:
        return "output contract not evaluated"
    first = violations[0]
    rest = f" (+{len(violations) - 1} more)" if len(violations) > 1 else ""
    return f"{first.get('code')}: {first.get('message')}{rest}"

"""Delegation lifecycle, ResultContracts and result validation (RFC 05 generic child work)."""

from __future__ import annotations

import json
import re
from typing import Any

from control.domain.digests import digest_of
from control.domain.errors import DomainError
from control.domain.lifecycle import Lifecycle, table

DELEGATION = Lifecycle(
    "delegation",
    table(
        {
            "pending": ("active", "failed", "cancelled"),
            "active": ("waiting_result", "succeeded", "failed", "cancelled"),
            "waiting_result": ("succeeded", "failed", "cancelled"),
        }
    ),
    frozenset({"succeeded", "failed", "cancelled"}),
)
ROLE_CONTRACT = {
    "review": "ReviewAssessment",
    "test": "TestResult",
    "research": "ResearchResult",
    "security": "ReviewAssessment",
    "integration": "IntegrationResult",
}
CONTRACT_KINDS = (
    "ReviewAssessment",
    "TestResult",
    "ResearchResult",
    "IntegrationResult",
    "GenericResult",
)
MAX_DEPTH = 3
MAX_CHILDREN = 10
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)

_SCHEMAS = {
    "ReviewAssessment": '{"kind":"ReviewAssessment","subject_digest":"<pinned digest>","verdict":"approve|request_changes|comment","findings":[{"severity":"info|minor|major|critical","message":"...","path":"optional","line":0}],"checks":[{"name":"...","status":"passed|failed|unknown"}]}',
    "TestResult": '{"kind":"TestResult","subject_digest":"<pinned digest>","status":"pass|fail|unknown","checks":[{"name":"...","command":"...","status":"passed|failed|unknown"}]}',
    "ResearchResult": '{"kind":"ResearchResult","summary":"...","sources":["..."]}',
    "IntegrationResult": '{"kind":"IntegrationResult","subject_digest":"<pinned digest>","status":"integrated|conflict|failed","notes":"..."}',
    "GenericResult": '{"kind":"GenericResult","summary":"..."}',
}


def build_contract(
    kind: str, *, subject_digest: str | None, platform_checks: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    if kind not in CONTRACT_KINDS:
        raise DomainError("validation_failed", f"unknown result contract kind {kind}")
    pins = {"subject_digest": subject_digest} if subject_digest else {}
    instructions = (
        f"When you finish, end your final reply with exactly one fenced ```json block that matches this "
        f"{kind} schema (the platform validates it; plain text is not a result):\n{_SCHEMAS[kind]}"
    )
    if subject_digest:
        instructions += f"\nThe subject_digest field MUST be exactly: {subject_digest}"
    contract = {
        "kind": kind,
        "schema_version": 1,
        "schema_digest": digest_of(_SCHEMAS[kind]),
        "enforcement": "platform_validation_prompt_output",
        "required_subject_pins": pins,
        "evidence_requirements": {"platform_checks": platform_checks or []},
        "completion_policy": "single_final_result",
        "instructions": instructions,
    }
    return contract


def extract_result(text: str) -> dict[str, Any] | None:
    blocks = _JSON_BLOCK.findall(text or "")
    if not blocks:
        return None
    try:
        value = json.loads(blocks[-1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def validate_result(contract: dict[str, Any], value: dict[str, Any] | None) -> dict[str, Any]:
    """Return typed gate fields; raise output_contract_invalid. Never infers approval."""
    if value is None:
        raise DomainError(
            "output_contract_invalid", "no fenced JSON result block in the completing Turn"
        )
    kind = contract["kind"]
    if value.get("kind") != kind:
        raise DomainError("output_contract_invalid", f"result kind must be {kind}")
    pinned = contract.get("required_subject_pins", {}).get("subject_digest")
    if pinned and value.get("subject_digest") != pinned:
        raise DomainError(
            "output_contract_invalid", "result is not pinned to the delegated subject"
        )
    verdict = None
    if kind == "ReviewAssessment":
        verdict = value.get("verdict")
        if verdict not in ("approve", "request_changes", "comment"):
            raise DomainError(
                "output_contract_invalid", "verdict must be approve, request_changes or comment"
            )
        findings = value.get("findings", [])
        if not isinstance(findings, list) or not all(
            isinstance(f, dict) and f.get("message") for f in findings
        ):
            raise DomainError("output_contract_invalid", "findings must be objects with a message")
    elif kind == "TestResult":
        verdict = value.get("status")
        if verdict not in ("pass", "fail", "unknown"):
            raise DomainError("output_contract_invalid", "status must be pass, fail or unknown")
        if not isinstance(value.get("checks", []), list):
            raise DomainError("output_contract_invalid", "checks must be a list")
    elif kind == "IntegrationResult":
        verdict = value.get("status")
        if verdict not in ("integrated", "conflict", "failed"):
            raise DomainError(
                "output_contract_invalid", "status must be integrated, conflict or failed"
            )
    return {
        "kind": kind,
        "verdict": verdict,
        "subject_digest": value.get("subject_digest") if pinned else None,
        "value": value,
    }

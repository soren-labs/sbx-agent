import json

from control.domain.errors import DomainError, require
from control.domain.events import digest

KINDS = {
    "review": "ReviewAssessment",
    "test": "TestResult",
    "research": "ResearchResult",
    "integration": "IntegrationResult",
}


def result_contract(role, subject, head):
    return {
        "kind": KINDS.get(role, "GenericResult"),
        "schema_version": 1,
        "enforcement": "platform",
        "subject_digest": subject,
        "head_sha": head,
        "required": ["subject_digest", "head_sha", "verdict", "findings", "checks"],
    }


def validate_result(text, contract):
    text = text.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    try:
        value = json.loads(text)
    except ValueError:
        raise DomainError("output_contract_invalid") from None
    require(
        isinstance(value, dict)
        and value.get("subject_digest") == contract["subject_digest"]
        and value.get("head_sha") == contract["head_sha"],
        "stale_subject",
    )
    require(
        value.get("kind") == contract["kind"] and all(k in value for k in contract["required"]),
        "output_contract_invalid",
    )
    allowed = {
        "ReviewAssessment": {"approve", "request_changes", "comment"},
        "TestResult": {"pass", "fail", "unknown"},
    }.get(contract["kind"], {"pass", "fail", "unknown"})
    require(
        value["verdict"] in allowed
        and isinstance(value["findings"], list)
        and isinstance(value["checks"], list),
        "output_contract_invalid",
    )
    for finding in value["findings"]:
        require(
            isinstance(finding, dict) and isinstance(finding.get("message"), str),
            "output_contract_invalid",
        )
    return value


def contract_digest(contract):
    return digest(contract)

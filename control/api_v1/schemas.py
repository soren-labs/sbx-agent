"""Pydantic request bodies and contract-shaped serializers for ``/v1``.

Field names follow ``docs/contracts/api-v1.yaml`` exactly: ``agent ≙ session``,
``run ≙ turn``. Provider is a ``Literal`` so an unknown value fails request
validation and surfaces as canonical ``400 invalid_provider``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ProviderId = Literal["codex", "antigravity", "grok", "opencode", "devin"]
VALID_SCOPES = ("agents", "admin")

USAGE_REQUIRED = ("input_tokens", "cached_input_tokens", "output_tokens")
USAGE_OPTIONAL = ("cache_write_input_tokens", "reasoning_output_tokens")


class Prompt(BaseModel):
    text: str = Field(min_length=1)


class AgentSpec(BaseModel):
    provider: ProviderId
    account_id: str | None = "auto"
    model: str | None = None


_COMMIT_SHA = r"^[0-9a-f]{40}$"


class WorkspaceDecl(BaseModel):
    """SOR-83 workspace declaration on agent create (``api-v1.yaml``)."""

    repo: str = Field(min_length=1)
    base_ref: str = Field(min_length=1)
    base_sha: str = Field(pattern=_COMMIT_SHA)


class HandoffRef(BaseModel):
    """SOR-83 cross-agent handoff reference: exactly one of the fields.

    ``artifact_id`` consumes a durable artifact package; ``head_sha`` checks
    out an exact commit in the declared repo. ``workspace`` is only used by
    ``POST /v1/agents/{id}/handoff`` when the agent has no recorded
    workspace yet.
    """

    artifact_id: str | None = None
    head_sha: str | None = Field(default=None, pattern=_COMMIT_SHA)
    workspace: WorkspaceDecl | None = None


class WorkflowMetadata(BaseModel):
    """Caller workflow binding for an agent (SOR-84 C1).

    ``task_id`` names the task inside ``workflow_id``; ``role`` is a
    free-form label (``worker`` / ``reviewer`` / ...); ``parent_task_id``
    optionally links a sub-task to its parent within the same workflow.
    """

    workflow_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=256)
    role: str = Field(min_length=1, max_length=64)
    parent_task_id: str | None = Field(default=None, max_length=256)


class CreateAgentRequest(BaseModel):
    prompt: Prompt
    agent: AgentSpec
    name: str | None = None
    idle_timeout_s: int | None = Field(default=None, ge=1)
    workspace: WorkspaceDecl | None = None
    handoff: HandoffRef | None = None
    metadata: WorkflowMetadata | None = None


class CreateRunRequest(BaseModel):
    prompt: Prompt
    # Optional task re-binding (SOR-84): a follow-up may carry the same
    # workflow metadata shape as agent create.
    metadata: WorkflowMetadata | None = None


class CreateArtifactRequest(BaseModel):
    """``POST /v1/agents/{id}/artifacts`` body (all optional)."""

    run_id: str | None = None
    test_command: str | None = None


class ReviewWorkspaceRequest(BaseModel):
    """``POST /v1/agents/{id}/workspace/review`` body.

    ``head_sha`` pins the exact commit reviewed; omitted means "the recorded
    head". A mismatch with the recorded head is ``head_sha_mismatch``.
    """

    head_sha: str | None = Field(default=None, pattern=_COMMIT_SHA)


class CreateAccountRequest(BaseModel):
    provider: ProviderId
    label: str = Field(min_length=1)
    credential: dict[str, Any] | None = None
    max_concurrent: int = Field(default=1, ge=1)
    models: list[str] = Field(default_factory=list)


class CreateApiKeyRequest(BaseModel):
    label: str = ""
    scopes: list[str] | None = None


def usage_public(usage: dict[str, Any] | None) -> dict[str, int] | None:
    """Usage with the three required fields defaulted to 0.

    ``None`` stays ``None``: usage that was never measured reports as
    unavailable (``null``), never as fabricated zeros (SOR-84).
    """
    if usage is None:
        return None
    out = {key: int(usage.get(key) or 0) for key in USAGE_REQUIRED}
    for key in USAGE_OPTIONAL:
        if key in usage and usage[key] is not None:
            out[key] = int(usage[key])
    return out


def agent_public(
    pub: dict[str, Any],
    meta: Any | None,
    *,
    usage: dict[str, Any] | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``api.yaml`` Session dict + ``AgentMeta`` -> ``api-v1.yaml`` Agent.

    ``usage`` is the session record's measured usage — pass
    ``SessionRecord.usage`` (``None`` while unmeasured → ``null``), not the
    zero-filled ``pub["usage"]`` the ``/api`` contract requires.
    ``metadata`` echoes the workflow binding when one is attached.
    """
    return {
        "id": pub["id"],
        "name": (meta.name if meta and meta.name else pub.get("title") or "untitled"),
        "provider": (meta.provider if meta else None) or pub.get("provider") or "codex",
        "account_id": (meta.account_id if meta else None) or pub.get("account_id") or "auto",
        "model": pub["model"],
        "status": pub["status"],
        "created_at": pub["created_at"],
        "updated_at": pub["updated_at"],
        "usage": usage_public(usage),
        "cost_estimate_usd": pub.get("cost_estimate_usd", 0.0),
        "metadata": metadata,
    }


def account_public(account: Any, running: int) -> dict[str, Any]:
    """``ports.Account`` -> ``api-v1.yaml`` Account (never credential material)."""
    return {
        "id": account.id,
        "provider": account.provider,
        "label": account.label,
        "status": account.status,
        "max_concurrent": account.max_concurrent,
        "running": running,
        "models": list(account.models),
        "created_at": account.created_at,
        "last_used_at": account.last_used_at,
        "cooldown_until": account.cooldown_until,
        "last_error": account.last_error,
    }


def api_key_public(key: Any) -> dict[str, Any]:
    """``ports.ApiKey`` -> ``api-v1.yaml`` ApiKey (hashes only, no plaintext)."""
    return {
        "id": key.id,
        "label": key.label,
        "scopes": list(key.scopes),
        "created_at": key.created_at,
        "revoked_at": key.revoked_at,
    }

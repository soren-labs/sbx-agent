from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Strict):
    email: str = Field(max_length=254)
    password: str = Field(min_length=8, max_length=1024, json_schema_extra={"writeOnly": True})


class ConnectionCreate(Strict):
    kind: Literal["modal", "github", "opencode_zen"]
    label: str = Field(default="", max_length=200)
    credential: dict[str, str] = Field(json_schema_extra={"writeOnly": True})


class CredentialReplace(Strict):
    expected_version: int
    credential: dict[str, str] = Field(json_schema_extra={"writeOnly": True})


class Version(Strict):
    expected_version: int


class ProjectCreate(Strict):
    name: str = Field(default="Project", max_length=200)
    repository: str
    base_ref: str = "main"
    spec: dict = Field(default_factory=dict)


class ProjectPublish(ProjectCreate):
    expected_version: int


class Message(Strict):
    content: str = Field(min_length=1, max_length=100000)
    routing: Literal["queue", "note", "steer"] = "queue"
    settings: dict = Field(default_factory=dict)


class SessionCreate(Strict):
    title: str = Field(default="New session", max_length=200)
    project_version_id: str | None = None
    repository: str | None = None
    base_ref: str = "main"
    provider_id: Literal["opencode"] = "opencode"
    backend: Literal["modal", "local"] = "modal"
    model: str = ""
    modal_connection_id: str | None = None
    github_connection_id: str | None = None
    zen_connection_id: str | None = None
    message: Message | None = None


class SessionPatch(Version):
    title: str = Field(max_length=200)


class Capture(Strict):
    generation: int = Field(ge=0)
    source_turn_id: str | None = None
    origin: Literal["automatic", "explicit", "salvage"] = "explicit"


class Generation(Strict):
    generation: int = Field(ge=0)


class DeliveryCreate(Strict):
    transport: Literal["pull_request", "git_branch", "export"] = "pull_request"
    github_connection_id: str | None = None
    base_ref: str = "main"
    automatic: bool = False


class Merge(Version):
    subject_digest: str
    expected_head: str
    expected_base: str | None = None
    method: Literal["squash", "merge", "rebase"] = "squash"


class Spawn(Strict):
    budget_seconds: int = Field(default=900, ge=1, le=1800)
    changeset_id: str
    role: Literal["review", "test", "research", "integration"] = "review"
    summary: str = Field(default="", max_length=10000)
    model: str | None = None


class Wait(Strict):
    seconds: int = Field(default=600, ge=1, le=3600)


class Publish(Strict):
    turn_id: str


class FileWrite(Generation):
    path: str
    content: str = Field(max_length=4000000)
    digest: str | None = None


class Apply(Generation):
    session_id: str
    subject_digest: str


class TerminalOpen(Strict):
    command: list[str] = Field(default_factory=lambda: ["/bin/bash", "--noprofile", "--norc"])


class PasswordChange(Strict):
    current_password: str
    password: str = Field(min_length=8, max_length=1024, json_schema_extra={"writeOnly": True})


class Receipt(BaseModel):
    model_config = ConfigDict(extra="allow")
    job_id: str | None = None
    operation_id: str | None = None
    event_watermark: int | None = None


class ConnectionView(Strict):
    id: str
    workspace_id: str
    kind: str
    label: str
    state: str
    version: int
    health: str


class TurnView(BaseModel):
    id: str
    ordinal: int
    session_id: str
    state: str
    evidence_complete: bool
    settings: dict
    outcome: dict | None = None
    reason: str | None = None


class WorktreeView(BaseModel):
    id: str
    generation: int
    availability: str
    last_snapshot_id: str | None = None
    base_sha: str | None = None


class MessageView(BaseModel):
    id: str
    content: str
    ordinal: int
    routing: str


class PartView(BaseModel):
    id: str
    execution_id: str
    kind: str
    revision: int
    content: str


class SessionView(BaseModel):
    id: str
    workspace_id: str
    creator_id: str
    title: str
    role: str
    lifecycle: str
    version: int
    provider_id: str
    project_version_id: str | None = None
    effective_inputs: dict
    event_watermark: int
    worktree: WorktreeView
    turns: list[TurnView]
    messages: list[MessageView]
    parts: list[PartView]


class EventView(BaseModel):
    id: str
    workspace_id: str
    session_id: str
    seq: int
    type: str
    schema_version: int
    recorded_at: str
    source: dict
    payload: dict
    turn_id: str | None = None
    execution_id: str | None = None


class ResultContract(Strict):
    verdicts: list[str]
    kind: str
    schema_version: int
    enforcement: Literal["platform"]
    subject_digest: str
    head_sha: str | None
    required: list[str]


class Continuation(Strict):
    summary: str = Field(min_length=1, max_length=100000)
    title: str = Field(default="Linked continuation", max_length=200)
    changeset_id: str | None = None


class EmailRequest(Strict):
    email: str = Field(min_length=3, max_length=254)


class PasswordReset(Strict):
    verifier: str = Field(min_length=1, max_length=200, json_schema_extra={"writeOnly": True})
    password: str = Field(min_length=8, max_length=1024, json_schema_extra={"writeOnly": True})

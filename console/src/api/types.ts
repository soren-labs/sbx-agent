/** Wire types for the unified `/api/...` business API (RFC 08). */

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

export interface ErrorBody {
  error: {
    code: string;
    category?: string;
    message?: string;
    retryable?: boolean;
    retry_after?: number;
    details?: Record<string, unknown> | null;
    request_id?: string;
    action?: string | null;
  };
}

// ---- identity -------------------------------------------------------------
export interface WorkspaceRef {
  id: string;
  name: string;
  kind: string;
}
export interface User {
  id: string;
  email: string;
  email_verified: boolean;
}
export interface Me {
  user: User;
  workspaces: WorkspaceRef[];
  auth: { via: string; scopes?: string[] };
}
export interface LoginResult extends Me {
  csrf_token: string;
  expires_at: string;
}
export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_at: string;
  revoked_at: string | null;
}
export interface CreatedApiKey extends ApiKey {
  /** Plaintext, returned exactly once. */
  key: string;
}

// ---- connections ----------------------------------------------------------
export type ConnectionKind = "modal" | "github" | "opencode_zen" | "codex";
export type ConnectionHealth =
  | "unverified"
  | "verifying"
  | "ready"
  | "degraded"
  | "reauth_required";
export type ConnectionState = "configured" | "disabled" | "revoked";

export type ConnectionCredential =
  | { token_id: string; token_secret: string }
  | { token: string }
  | { api_key: string }
  | { auth_json: string };

export interface CatalogModel {
  id: string;
  free?: boolean;
  usable_via?: string | string[];
}
export interface Connection {
  id: string;
  workspace_id?: string;
  kind: ConnectionKind;
  label: string;
  state: ConnectionState;
  health: ConnectionHealth;
  health_reason: string | null;
  external_identity: string | null;
  version: number;
  credential: { id: string; ordinal: number; format: string; created_at: string } | null;
  validation: {
    status: string;
    observed_at: string;
    details?: Record<string, unknown> | null;
    quota_consuming?: boolean;
  } | null;
  catalog: {
    observed_at?: string;
    preferred_model?: string | null;
    models?: CatalogModel[];
  } | null;
  created_at?: string;
  updated_at?: string;
}

// ---- catalog --------------------------------------------------------------
export interface Harness {
  provider_id: string;
  support_tier: string;
  capabilities: Record<string, { status: string }>;
}
export interface ModelsView {
  provider_id: string;
  preferred_model: string | null;
  connections: {
    connection_id: string;
    label: string;
    health: ConnectionHealth;
    preferred_model?: string | null;
    observed_at?: string | null;
    models: { id: string; free?: boolean }[];
  }[];
}
export interface ExecutorBackend {
  kind: string;
  [k: string]: unknown;
}

// ---- projects -------------------------------------------------------------
export interface ProjectSpec {
  repository: { full_name: string; base_ref: string };
  checks?: { name: string; argv: string[] }[];
  defaults?: {
    harness?: { provider_id: string; model?: string };
    executor?: { backend: string };
  };
}
export interface ProjectVersion {
  id: string;
  project_id: string;
  ordinal: number;
  spec: ProjectSpec;
  spec_digest: string;
  created_at: string;
}
export interface Project {
  id: string;
  workspace_id: string;
  slug: string;
  name: string;
  version: number;
  current_version: ProjectVersion | null;
  created_at: string;
  updated_at: string;
}

// ---- sessions -------------------------------------------------------------
export type Lifecycle = "open" | "archived" | "closed";
export interface Session {
  id: string;
  workspace_id?: string;
  lifecycle: Lifecycle;
  activity: string;
  role: string;
  title: string;
  labels?: string[];
  project_id?: string | null;
  harness: { provider_id: string; model: string | null };
  executor: {
    backend: string;
    resource_class?: string;
    lease_id: string | null;
    lease_state: string | null;
  };
  worktree: {
    id?: string;
    availability: string;
    generation: number;
    repository?: string | null;
    base_sha: string | null;
    recovery_point?: unknown;
  } | null;
  parent_session_id: string | null;
  active_turn_id?: string | null;
  actions: string[];
  version: number;
  created_at?: string;
  updated_at?: string;
}
export interface SessionList {
  items: Session[];
  next_cursor: string | null;
}
export interface SessionSnapshot {
  session: Session;
  event_watermark: number;
}
export interface CreateSessionBody {
  project_id?: string;
  harness: { provider_id: string; model?: string };
  executor: { backend: string };
  repository?: { full_name: string; base_ref: string };
  title?: string;
  message?: { content: string };
}
export interface CreateSessionResult {
  session_id: string;
  turn_id?: string;
  session: Session;
  event_watermark: number;
}

export interface MessagePart {
  id?: string;
  key: string;
  kind: "text" | "reasoning" | "tool" | string;
  ordinal?: number;
  revision: number;
  content: string;
  data?: Record<string, unknown> | null;
  sealed?: boolean;
}
export interface Message {
  id: string;
  ordinal: number;
  role: "user" | "assistant" | "system";
  author_kind?: string;
  routing?: string;
  content: { kind: string; text?: string }[];
  turn_id: string | null;
  state: string;
  parts: MessagePart[];
  created_at?: string;
}
export interface MessageList {
  items: Message[];
  event_watermark: number;
}
export interface Turn {
  id: string;
  ordinal: number;
  state: string;
  reason: string | null;
  retry_of_turn_id: string | null;
  error: { code: string; message: string } | null;
  outcome: unknown;
  evidence_complete?: boolean;
  actions: string[];
  version: number;
  created_at?: string;
  finished_at?: string | null;
}
export interface Accepted {
  message_id?: string;
  turn_id?: string;
  operation_id?: string;
  job_id?: string;
  event_watermark?: number;
  [k: string]: unknown;
}

// ---- events ---------------------------------------------------------------
export interface EventEnvelope {
  id: string;
  seq: number;
  type: string;
  session_id?: string;
  turn_id?: string | null;
  execution_id?: string | null;
  recorded_at?: string | null;
  payload: Record<string, unknown>;
  [k: string]: unknown;
}
export interface EventPage {
  items: EventEnvelope[];
  next_after: number;
  event_watermark: number;
}

// ---- executor -------------------------------------------------------------
export interface ExecutorView {
  backend: string;
  leases: { id: string; generation: number; state: string; reason: string | null; quarantined?: boolean }[];
  worktree: { id?: string; availability: string; generation: number; base_sha: string | null };
  recovery_point: { snapshot_id: string; generation: number; created_at: string } | null;
  event_watermark?: number;
}

// ---- changes --------------------------------------------------------------
export interface LiveChanges {
  live: boolean;
  observation: {
    generation?: number;
    head?: string;
    files: { path: string; status: string }[];
  };
}
export interface ChangeSetFile {
  path: string;
  type: string;
  mode?: string;
  digest?: string | null;
}
export interface ChangeSet {
  id: string;
  session_id: string;
  source_turn_id: string | null;
  state: string;
  origin: string;
  subject_digest: string | null;
  repository: string | null;
  base_sha: string | null;
  head_sha: string | null;
  worktree_generation: number | null;
  file_count: number | null;
  error: unknown;
  created_at: string;
  sealed_at: string | null;
  files?: ChangeSetFile[];
}
export interface Delivery {
  id: string;
  session_id: string;
  changeset_id: string;
  subject_digest: string | null;
  transport: string;
  repository: string | null;
  target_ref: string | null;
  base_branch: string | null;
  draft: boolean;
  state: string;
  state_reason: string | null;
  commit_sha: string | null;
  pull_request: { number: number; url: string; draft: boolean; state: string } | null;
  remote?: { head_sha: string | null; checks_state: string | null; observed_at: string | null };
  steps: { kind: string; outcome: string; evidence?: unknown; created_at?: string }[];
  merge_requests: { id: string; state: string; gate?: unknown; merge_sha: string | null; error?: unknown }[];
  merge_eligibility: {
    eligible: boolean;
    reasons: string[];
    subject_digest: string | null;
    head_sha: string | null;
    observed_at: string | null;
  };
  version: number;
}
export interface MergeRequestBody {
  expected_head_sha: string;
  subject_digest: string;
  expected_version: number;
  method: "squash";
  mark_ready?: boolean;
}

// ---- delegations ----------------------------------------------------------
export type DelegationRole = "review" | "test" | "research" | "security" | "integration";
export interface Delegation {
  id: string;
  parent_session_id: string;
  child_session_id: string;
  child?: { lifecycle: string; harness: { provider_id: string; model: string | null } };
  role: string;
  state: string;
  state_reason?: string | null;
  subject: { changeset_id: string | null; subject_digest: string | null };
  result: {
    id?: string;
    kind: string;
    verdict: string | null;
    independent: boolean;
    subject_digest: string | null;
    validation_status?: string;
    value: unknown;
  } | null;
  created_at?: string;
}

// ---- files / terminal / services -----------------------------------------
export interface FileEntry {
  path: string;
  type: string;
  size?: number;
}
export interface FileContent {
  path: string;
  encoding: string;
  content: string;
  digest: string;
}
export interface TerminalOutput {
  data: string;
  offset: number;
  closed: boolean;
}
export interface ServiceItem {
  name: string;
  desired: string;
  state: string;
  port: number | null;
  preview: boolean | string | null;
  lease_live?: boolean;
}

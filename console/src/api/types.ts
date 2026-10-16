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
/** Kinds a user can create. Inference is harness-neutral bring-your-own-key. */
export type ConnectionKind = "modal" | "github" | "inference_api";
/** Vendor-specific kinds retired from new flows; stored ones are listed read-only. */
export type LegacyConnectionKind = "opencode_zen" | "codex";
export type InferenceProtocol = "openai_chat" | "openai_responses" | "anthropic_messages";
export interface InferenceCredential {
  api_key: string;
  model: string;
  endpoints: Partial<Record<InferenceProtocol, string>>;
  models?: string[];
}
/** Non-secret settings of an inference Connection (never includes the key). */
export interface InferenceConfig {
  endpoints?: Partial<Record<InferenceProtocol, string>>;
  model?: string;
  models?: string[];
}
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
  | InferenceCredential;

export interface CatalogModel {
  id: string;
  /** Reasoning values the selected Harness may send for this model (verified); usually empty. */
  efforts?: string[];
  free?: boolean;
  usable_via?: string | string[];
}
export interface Connection {
  id: string;
  workspace_id?: string;
  kind: ConnectionKind | LegacyConnectionKind;
  label: string;
  /** True for retired vendor-specific kinds: disconnect only. */
  legacy?: boolean;
  config?: InferenceConfig;
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
    protocols?: InferenceProtocol[];
    source?: string;
  } | null;
  created_at?: string;
  updated_at?: string;
}

// ---- catalog --------------------------------------------------------------
export interface Harness {
  provider_id: string;
  support_tier: string;
  cli_version?: string;
  distribution?: string;
  /** Wire protocols the official CLI can be pointed at, in preference order. */
  inference_protocols?: InferenceProtocol[];
  capabilities: Record<string, { status: string; evidence?: string; limitation?: string }>;
}
export interface ModelsView {
  provider_id: string;
  inference_protocols?: InferenceProtocol[];
  preferred_model: string | null;
  connections: {
    connection_id: string;
    label: string;
    health: ConnectionHealth;
    preferred_model?: string | null;
    observed_at?: string | null;
    models: CatalogModel[];
    protocols?: InferenceProtocol[];
    /** Endpoint the selected Harness would use; null when it offers none. */
    protocol?: InferenceProtocol | null;
    compatible?: boolean;
  }[];
}
export interface ExecutorBackend {
  kind: string;
  [k: string]: unknown;
}

// ---- projects -------------------------------------------------------------
/** A declared check: an exec argv, never run through a shell. */
export interface ProjectCheck {
  name: string;
  argv: string[];
  timeout_seconds?: number;
  [extra: string]: unknown;
}
/** A declared service (the Services tab lists exactly these). */
export interface ProjectService {
  name: string;
  argv: string[];
  cwd?: string;
  port?: number | null;
  health?: { path?: string } | null;
  restart?: string;
  order?: number;
  preview?: boolean;
  [extra: string]: unknown;
}
export interface ShipPolicy {
  transport?: string;
  base_branch?: string | null;
  draft?: boolean;
  automatic_delivery?: boolean;
  required_results?: { kind: string; count?: number; verdict?: string }[];
  required_checks?: string[];
  independence?: string;
  merge_methods?: string[];
  require_base_unchanged?: boolean;
  [extra: string]: unknown;
}
/**
 * A complete ProjectVersion spec as stored (schema 1). Publishing replaces it whole, so
 * the Console derives a new version from the current one; fields it does not edit, and
 * fields it does not know, are carried over unchanged.
 */
export interface ProjectSpec {
  schema_version?: number;
  repository: { full_name: string; base_ref: string; provider?: string; clone_url?: string; [extra: string]: unknown } | null;
  environment?: { env?: Record<string, string>; [extra: string]: unknown };
  checks?: ProjectCheck[];
  services?: ProjectService[];
  defaults?: {
    harness?: { provider_id: string; model?: string; effort?: string; [extra: string]: unknown };
    executor?: { backend: string; resource_class?: string; [extra: string]: unknown };
    turn_defaults?: Record<string, unknown>;
    [extra: string]: unknown;
  };
  ship_policy?: ShipPolicy;
  [extra: string]: unknown;
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
  harness: { provider_id: string; model: string | null; effort?: string | null };
  inference?: { mode: "custom_api" | "subscription"; machine_slot_id: string | null };
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
  harness: { provider_id: string; model?: string; effort?: string };
  /** Subscription: run on a Machine Slot instead of an inference API key. */
  inference?: { mode: "subscription"; machine_slot_id: string };
  connections?: { inference?: string };
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
  /** When the part first appeared / last changed; null for parts older than the field. */
  created_at?: string | null;
  updated_at?: string | null;
  /** Client-only: when this browser last applied a live change to the part (ms). */
  seen_at?: number;
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
  /** Token usage the CLI reported for this Turn; absent when it reported none. */
  usage?: Record<string, unknown> | null;
  evidence_complete?: boolean;
  actions: string[];
  version: number;
  created_at?: string;
  started_at?: string | null;
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
  /** Server-evaluated: the same check gates the delivery command. */
  delivery?: DeliveryCapability;
}
export type DeliveryBlockReason =
  | "not_ready"
  | "no_repository"
  | "no_baseline"
  | "transport_unsupported"
  | "connection_required"
  | "connection_revoked";
export interface DeliveryCapability {
  available: boolean;
  reason: DeliveryBlockReason | null;
  message: string | null;
  transport: string;
  repository: string | null;
  base_branch: string | null;
  draft: boolean;
  connection_id: string | null;
  /** Actions that really exist for this ChangeSet when delivery is not available. */
  recovery: ("download_patch" | "manage_connections")[];
  patch_available: boolean;
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
  /** False while this deployment has no preview origin (see `capabilities.preview`). */
  preview_available?: boolean;
  observed_at?: string | null;
  lease_live?: boolean;
}
/** Where a Session's services are declared: its pinned ProjectVersion, or nowhere. */
export interface ServiceSource {
  kind: "project_version" | "none";
  project_id: string | null;
  project_name: string | null;
  project_version_id: string | null;
  project_version_ordinal: number | null;
  current_version_id: string | null;
}
export interface ServiceList {
  items: ServiceItem[];
  source?: ServiceSource;
  capabilities?: { preview?: { enabled: boolean; reason?: string } };
  event_watermark?: number;
}
export interface ServiceLogs {
  name?: string;
  lines: string[];
  /** False when the service has not been started on the current compute yet. */
  started?: boolean;
}

// -- Machine Slots (subscription logins) ---------------------------------------------------
export type SlotStatus = "running" | "ready" | "login_pending" | "needs_login" | "error" | "deleting";

export interface SlotEffort {
  id: string;
  description: string | null;
}
export interface SlotModel {
  id: string;
  name: string;
  description: string | null;
  default: boolean;
  reasoning: { kind: "levels" | "none"; efforts: SlotEffort[]; default: string | null };
}
/** The model catalog the Slot's authenticated CLI reported; never a built-in list. */
export interface SlotCatalog {
  status: "ready" | "unavailable";
  source: string | null;
  observed_at: string;
  cli_version: string | null;
  models: SlotModel[];
  default_model: string | null;
  complete?: boolean;
}
/** device_code: confirm a shown code; authorization_code: send back the code the browser shows. */
export type LoginMethod = "device_code" | "authorization_code";
export interface SlotLogin {
  attempt_id: string;
  mode: "login" | "verify";
  state: "starting" | "awaiting_user" | "verifying" | "succeeded" | "failed" | "expired" | "cancelled";
  /** How the provider's official login works; see SubscriptionProvider.login_method. */
  method?: LoginMethod | null;
  verification_url: string | null;
  /** One-time code; present only while it still has to be entered. */
  user_code: string | null;
  /** Set while the login waits for the code the browser displays after sign-in. */
  input?: "authorization_code" | null;
  code_expires_at: string | null;
  error_code: string | null;
  started_at: string;
  finished_at: string | null;
}
export interface MachineSlot {
  id: string;
  workspace_id: string;
  provider: string;
  provider_name: string;
  label: string;
  account_alias: string | null;
  state: string;
  status: SlotStatus;
  state_reason: string | null;
  busy: boolean;
  worker: { lease_id: string; session_id: string } | null;
  compute_connection_id: string;
  volume: { name: string; filesystem: string; managed: boolean };
  login: SlotLogin | null;
  capabilities: {
    catalog?: SlotCatalog;
    cli_version?: string | null;
    verification?: { real_model_call: boolean; observed_at: string };
  };
  verified_at: string | null;
  last_used_at: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}
export interface SubscriptionProvider {
  provider_id: string;
  display_name: string;
  harness_provider_id: string;
  login_method: LoginMethod | string;
  verification_host: string;
  login_window_seconds: number;
  catalog_source: string | null;
  available: boolean;
}
export interface MachineSlotList {
  items: MachineSlot[];
  summary: { total: number; running: number; ready: number; login_pending: number; needs_attention: number };
  providers: SubscriptionProvider[];
}

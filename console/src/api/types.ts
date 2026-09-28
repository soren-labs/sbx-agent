/**
 * V2 Session Console — product-level contract types.
 *
 * Product vocabulary is Session / Turn / Activity / Change. The wire
 * contract is the merged V2 Session API (``/v2/sessions*`` — SessionView /
 * RunView / RevisionView projections, session-scoped SSE) plus the allowed
 * V1 surfaces for providers, models and GitHub App state. All mapping lives
 * in normalize.ts / http.ts so the UI never sees backend nouns. Nothing
 * here invents backend semantics.
 */

export type ProviderId = "codex" | "antigravity" | "grok" | "opencode" | "devin";

/** Canonical reasoning effort (SOR-179), shared by every provider surface. */
export type EffortLevel =
  | "none"
  | "minimal"
  | "low"
  | "medium"
  | "high"
  | "xhigh"
  | "max";

/**
 * Product-level session lifecycle shown to users. Derived from the wire
 * ``phase`` (provisioning|queued|running|delivering|finished|failed|cancelled):
 * provisioning→starting, delivering→running, finished→idle, cancelled→ended.
 */
export type SessionPhase =
  | "queued"
  | "starting"
  | "running"
  | "idle"
  | "ended"
  | "failed";

/** The wire ``status`` — the only values a V2 client switches on. */
export type SessionStatus =
  | "queued"
  | "running"
  | "finished"
  | "failed"
  | "cancelled";

export type SessionEndReason = "cancelled" | "failed" | null;

/** Wire RunView.status / SessionStatus — turn-level lifecycle. */
export type TurnStatus =
  | "queued"
  | "running"
  | "finished"
  | "failed"
  | "cancelled";

export interface RepoRef {
  /** Display form, e.g. "owner/repo". Never a raw internal id. */
  name: string;
  url?: string;
  ref?: string;
  baseSha?: string;
}

export interface Usage {
  inputTokens: number;
  cachedInputTokens: number;
  outputTokens: number;
  cacheWriteInputTokens?: number;
  reasoningOutputTokens?: number;
}

export interface TurnError {
  /** Canonical error code (error_catalog / run error codes). */
  code: string;
  source?: "provider" | "runtime" | "control" | "telemetry";
  message: string;
  retryable: boolean;
  retryAfter?: number;
}

export type ActivityKind =
  | "status" // phase transitions: queued/starting/running/finished
  | "message" // user/assistant conversation text
  | "reasoning"
  | "command"
  | "file_change"
  | "error"
  | "info"; // misc normalized notes (keepalives never reach here)

export interface ActivityItem {
  /** Stable id — item.* frames share the canonical item id so a
   * completed row replaces its started placeholder (no duplicates). */
  id: string;
  /** Monotonic sequence for stable ordering (SSE line number). */
  seq: number;
  ts: string;
  /** Turn the item belongs to — "turn-<n>" matching Turn.id, or null. */
  turnId: string | null;
  /** Session-relative turn number the frame was annotated with. */
  n?: number;
  kind: ActivityKind;
  role?: "user" | "assistant" | "system";
  text?: string;
  status?: string;
  command?: string;
  exitCode?: number;
  output?: string;
  /** Single-path convenience (first of `changes`). */
  path?: string;
  changeType?: "added" | "modified" | "deleted";
  /** Canonical file_change payload: every touched path + kind. */
  changes?: { path: string; kind: string }[];
  error?: TurnError;
}

export interface Turn {
  /** "turn-<n>" — the session-relative turn number as a stable key. */
  id: string;
  /** 1-based conversation index (the wire ``n``). */
  index: number;
  prompt: string;
  status: TurnStatus;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  result: string | null;
  error: TurnError | null;
  usage?: Usage | null;
  queuePosition?: number | null;
  provider?: string | null;
  model?: string | null;
  effort?: string | null;
  activity: ActivityItem[];
}

export interface ComputeSpec {
  cpu: [number, number];
  memoryMib: [number, number];
}

export type DeliveryMode = "none" | "branch" | "pr" | "draft_pr";

export interface SessionDelivery {
  mode: DeliveryMode;
  /** pending | delivered | failed — wire DeliveryView.status. */
  status?: string;
  branch?: string;
  /** Head sha the delivery last pushed — stale-PR detection. */
  pushedHeadSha?: string;
  prUrl?: string;
  prNumber?: number;
  prState?: string;
  prHeadSha?: string;
  prBase?: string;
  /** Wire DeliveryView.error — {code,message} or a plain message. */
  error?: { code?: string; message?: string } | string;
}

/** The wire ``changes`` view (ChangesView) — where the produced work lives. */
export interface SessionChangeInfo {
  status: string;
  baseSha?: string;
  headSha?: string;
  branch?: string;
}

export interface Session {
  id: string;
  title: string;
  status: SessionStatus;
  phase: SessionPhase;
  endReason: SessionEndReason;
  prompt: string;
  provider: ProviderId | string | null;
  model: string | null;
  /** Human label only — raw account ids never reach the UI. */
  accountLabel: string | null;
  repo: RepoRef | null;
  effort: string | null;
  compute: ComputeSpec | null;
  idleTimeoutS: number | null;
  delivery: SessionDelivery | null;
  /** Workspace change state (base/head shas live here for Details). */
  changes?: SessionChangeInfo | null;
  createdAt: string;
  updatedAt: string;
  usage: Usage | null;
  costUsd: number | null;
  /** Number of turns the session has run (the wire ``turns`` count). */
  turnCount: number;
  turns: Turn[];
  /** Last activity across all turns, for list previews. */
  lastActivityPreview: string | null;
  hasChanges: boolean;
  error: TurnError | null;
}

/** Wire readiness vocabulary (SOR-258) folded from runtime + connection. */
export type ProviderReadiness =
  | "ready"
  | "needs_login"
  | "busy"
  | "disabled"
  | "unhealthy";

export interface ProviderInfo {
  id: ProviderId | string;
  label: string;
  support?: string;
  readiness: ProviderReadiness | string;
  /** Catalog ``default_models`` — what Auto may pick from. */
  models: string[];
  runtimeStatus: "ready" | "degraded" | "unknown" | "disabled" | string;
  runtimeEnabled: boolean;
  connectionStatus: "not_connected" | "connected" | "degraded" | string;
  connectionDetail?: string;
  accountsTotal: number;
  accountsAvailable: number;
  needsLogin: boolean;
}

/** One /v1/models row — the only agents-scope surface listing account ids. */
export interface ModelInfo {
  provider: string;
  model: string;
  displayName?: string;
  /** Real account id the model was discovered on (safe to submit). */
  account?: string;
  accountsAvailable: number;
  availability?: "available" | "busy" | "unavailable" | string;
  reasoningEfforts: EffortLevel[];
  defaultEffort?: EffortLevel;
}

export interface SessionChange {
  id: string;
  kind: "workspace" | "revision" | "delivery" | "file";
  /** Revision sequence number when the row is a revision. */
  n?: number;
  status?: string;
  /** The revision's delivery lifecycle (pending|delivered|failed). */
  deliveryStatus?: string;
  summary: string;
  ts: string;
  branch?: string;
  headSha?: string;
  url?: string;
  /** PR number when the row's delivery produced one. */
  prNumber?: number;
  ref?: string;
  path?: string;
  changeType?: "added" | "modified" | "deleted";
  error?: string;
}

export type ChangeFileStatus = "added" | "modified" | "deleted" | "renamed";

/** One file row of a revision's parsed patch (GET .../changes/diff). */
export interface SessionDiffFile {
  path: string;
  status: ChangeFileStatus;
  additions: number;
  deletions: number;
  /** Present on a rename — the previous path. */
  oldPath?: string;
  /** Unified diff body — only populated by the per-file lazy fetch. */
  diff?: string | null;
}

/** Parsed patch summary — file list + totals, no diff bodies. */
export interface SessionChangesDiff {
  n: number;
  baseSha?: string;
  headSha?: string;
  filesChanged: number;
  additions: number;
  deletions: number;
  files: SessionDiffFile[];
}

/** A single file's diff section, fetched lazily (?path=). */
export interface SessionFileDiff extends SessionDiffFile {
  diff: string;
}

/** Explicit POST .../deliver input — the console's "Create pull request". */
export interface DeliverInput {
  /** PR title — defaults to the session title server-side. */
  title?: string;
  /** Open as a draft pull request. */
  draft?: boolean;
  /** PR base branch — defaults to the session's repo ref. */
  target?: string;
}

/** POST .../deliver result — refreshed session + the delivered revision. */
export interface SessionDeliverResult {
  session: Session;
  revision: SessionChange;
}

export interface IntegrationStatus {
  providers: ProviderInfo[];
  github: {
    configured: boolean;
    installable: boolean;
    connected: boolean;
    /** account_login of each installation (metadata only). */
    accounts: string[];
    appSlug?: string;
    source?: string;
  };
  runtime: { enabled: boolean; backend?: string };
}

/** Composer payload — what the user can set. Never internal ids. */
export interface NewSessionInput {
  prompt: string;
  repo?: string;
  repoRef?: string;
  provider?: ProviderId | "auto" | string;
  model?: string;
  title?: string;
  effort?: EffortLevel | string;
  /** A real account id (from /v1/models) or "auto". */
  account?: string;
  delivery?: DeliveryMode;
  /** PR base ref when delivery is pr/draft_pr. */
  deliveryTarget?: string;
  compute?: { cpu?: number; memoryMib?: number };
  secrets?: string[];
  mcpServers?: string[];
  idleTimeoutS?: number;
}

export type ErrorKind =
  | "provider_login"
  | "provider_busy"
  | "runtime_disabled"
  | "github_required"
  | "session_failed"
  | "unauthorized"
  | "not_found"
  | "conflict"
  | "network"
  | "unknown";

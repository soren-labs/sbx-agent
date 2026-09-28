/**
 * V2 Session Console — product-level contract types.
 *
 * Product vocabulary is Session / Turn / Activity / Change. The backend
 * contract (docs/contracts/api-v1.yaml) speaks agent ≙ session, run ≙ turn;
 * all mapping lives in normalize.ts / http.ts so the UI never sees backend
 * nouns. Nothing here invents backend semantics — fields mirror the frozen
 * contract, renamed into product terms.
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
 * Product-level session lifecycle. The UI tracks queued → starting →
 * running ⇄ idle → ended/failed. Backend `creating` normalizes to
 * `starting`; `closed`/`timed_out`/`lost` normalize to `ended` with an
 * endReason; a terminal run error normalizes to `failed`.
 */
export type SessionPhase =
  | "queued"
  | "starting"
  | "running"
  | "idle"
  | "ended"
  | "failed";

export type SessionEndReason = "closed" | "timed_out" | "lost" | "failed" | null;

export type TurnStatus =
  | "queued"
  | "running"
  | "finished"
  | "error"
  | "cancelled";

export interface RepoRef {
  /** Display form, e.g. "owner/repo". Never a raw internal id. */
  name: string;
  url?: string;
  ref?: string;
}

export interface Usage {
  inputTokens: number;
  cachedInputTokens: number;
  outputTokens: number;
  cacheWriteInputTokens?: number;
  reasoningOutputTokens?: number;
}

export interface TurnError {
  /** Canonical run_error_codes / error_subcodes value. */
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
  id: string;
  /** Monotonic sequence for stable ordering (SSE line number). */
  seq: number;
  ts: string;
  turnId: string | null;
  kind: ActivityKind;
  role?: "user" | "assistant" | "system";
  text?: string;
  status?: string;
  command?: string;
  exitCode?: number;
  output?: string;
  path?: string;
  changeType?: "added" | "modified" | "deleted";
  error?: TurnError;
}

export interface Turn {
  id: string;
  /** 1-based conversation index. */
  index: number;
  prompt: string;
  status: TurnStatus;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  result: string | null;
  error: TurnError | null;
  activity: ActivityItem[];
}

export interface ComputeSpec {
  cpu: [number, number];
  memoryMib: [number, number];
}

export type DeliveryMode = "none" | "branch" | "pr" | "draft_pr";

export interface Session {
  id: string;
  title: string;
  phase: SessionPhase;
  endReason: SessionEndReason;
  provider: ProviderId;
  model: string;
  /** Human label only — raw account ids never reach the UI. */
  accountLabel: string | null;
  repo: RepoRef | null;
  effort: EffortLevel | null;
  compute: ComputeSpec | null;
  idleTimeoutS: number | null;
  delivery: { mode: DeliveryMode; target?: string } | null;
  createdAt: string;
  updatedAt: string;
  usage: Usage | null;
  costUsd: number | null;
  runtimeSeconds: number | null;
  turns: Turn[];
  /** Last activity across all turns, for list previews. */
  lastActivityPreview: string | null;
  hasChanges: boolean;
  error: TurnError | null;
}

export interface ProviderInfo {
  id: ProviderId;
  label: string;
  models: string[];
  efforts: EffortLevel[];
  accountsTotal: number;
  accountsAvailable: number;
  needsLogin: boolean;
}

export interface SessionChange {
  id: string;
  kind: "file" | "revision" | "delivery";
  summary: string;
  ts: string;
  path?: string;
  changeType?: "added" | "modified" | "deleted";
  ref?: string;
  url?: string;
}

export interface IntegrationStatus {
  providers: ProviderInfo[];
  github: {
    configured: boolean;
    connected: boolean;
    account?: string;
    installUrl?: string;
  };
  runtime: { enabled: boolean; backend?: string };
}

/** Composer payload — what the user can set. Never internal ids. */
export interface NewSessionInput {
  prompt: string;
  repo?: string;
  provider?: ProviderId | "auto";
  model?: string;
  title?: string;
  effort?: EffortLevel;
  account?: string;
  delivery?: DeliveryMode;
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

import type {
  ActivityItem,
  ErrorKind,
  SessionPhase,
  TurnError,
  TurnStatus,
} from "./types";
import { ApiError } from "./client";

/**
 * Backend → product normalization. All backend nouns (agent/run/item,
 * status enums, error subcodes) are translated here so components only
 * handle product vocabulary.
 */

const PHASE_MAP: Record<string, SessionPhase> = {
  queued: "queued",
  creating: "starting",
  starting: "starting",
  running: "running",
  idle: "idle",
  suspended: "idle",
  closed: "ended",
  timed_out: "ended",
  lost: "ended",
  failed: "failed",
};

export function normalizePhase(raw: string | undefined | null): SessionPhase {
  if (!raw) return "queued";
  return PHASE_MAP[raw] ?? "queued";
}

export function endReasonOf(
  raw: string | undefined | null,
): "closed" | "timed_out" | "lost" | "failed" | null {
  if (raw === "closed") return "closed";
  if (raw === "timed_out") return "timed_out";
  if (raw === "lost") return "lost";
  if (raw === "failed") return "failed";
  return null;
}

const TURN_MAP: Record<string, TurnStatus> = {
  CREATING: "queued",
  QUEUED: "queued",
  RUNNING: "running",
  FINISHED: "finished",
  ERROR: "error",
  CANCELLED: "cancelled",
  EXPIRED: "cancelled",
  UNKNOWN: "error",
};

export function normalizeTurnStatus(raw: string | undefined | null): TurnStatus {
  if (!raw) return "queued";
  return TURN_MAP[raw.toUpperCase()] ?? "queued";
}

/** Canonical subcode/run-error → product-level error kind. */
export function toErrorKind(
  subcode: string | undefined | null,
  httpStatus?: number,
): ErrorKind {
  const code = (subcode ?? "").toLowerCase();
  if (
    code === "auth_invalid" ||
    code === "provider_auth_required" ||
    code === "account_unverified" ||
    code === "connect_failed" ||
    code === "grant_invalid"
  ) {
    return "provider_login";
  }
  if (
    code === "account_busy" ||
    code === "account_unavailable" ||
    code === "provider_exhausted" ||
    code === "concurrency_limit" ||
    code === "rate_limited" ||
    code === "quota_exhausted" ||
    code === "model_capacity" ||
    code === "model_unavailable" ||
    code === "provider_unavailable"
  ) {
    return "provider_busy";
  }
  if (
    code === "runtime_disabled" ||
    code === "runtime_unavailable" ||
    code === "backend_disabled" ||
    code === "runtime_error"
  ) {
    return "runtime_disabled";
  }
  if (
    code === "github_required" ||
    code === "github_app_unconfigured" ||
    code === "git_required" ||
    code === "github_app_invalid"
  ) {
    return "github_required";
  }
  if (code === "session_failed" || code === "session_not_runnable") {
    return "session_failed";
  }
  if (code === "unauthorized" || httpStatus === 401) return "unauthorized";
  if (code === "not_found" || httpStatus === 404) return "not_found";
  if (
    code === "turn_in_progress" ||
    code === "idempotency_conflict" ||
    httpStatus === 409
  ) {
    return "conflict";
  }
  if (code === "timeout" || code === "cancelled") return "session_failed";
  if (code === "network" || code === "fetch_failed") return "network";
  return httpStatus && httpStatus >= 500 ? "network" : "unknown";
}

export function toApiError(
  e: unknown,
  fallbackMessage = "Request failed",
): ApiError {
  if (e instanceof ApiError) return e;
  if (e instanceof TypeError) {
    return new ApiError("network", "Cannot reach the control plane", {
      subcode: "network",
      retryable: true,
    });
  }
  return new ApiError(
    "unknown",
    e instanceof Error ? e.message : fallbackMessage,
  );
}

export function toTurnError(raw: unknown): TurnError | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  return {
    code: typeof r.code === "string" ? r.code : "runtime_error",
    source:
      r.source === "provider" ||
      r.source === "runtime" ||
      r.source === "control" ||
      r.source === "telemetry"
        ? r.source
        : undefined,
    message: typeof r.message === "string" ? r.message : "Turn failed",
    retryable: Boolean(r.retryable),
    retryAfter: typeof r.retry_after === "number" ? r.retry_after : undefined,
  };
}

/**
 * Normalize one raw stream event (canonical events.md / codex-style frame)
 * into an ActivityItem. Returns null for frames the UI should ignore
 * (keepalives, heartbeats, unrecognized envelopes).
 */
let seqCounter = 0;
export function normalizeEvent(raw: unknown, idHint?: string): ActivityItem | null {
  if (!raw || typeof raw !== "object") return null;
  const frame = raw as Record<string, unknown>;
  const type = typeof frame.type === "string" ? frame.type : "";
  const ts =
    typeof frame.ts === "string" ? frame.ts : new Date().toISOString();
  const seq =
    typeof frame.seq === "number"
      ? frame.seq
      : idHint && !Number.isNaN(Number(idHint))
        ? Number(idHint)
        : ++seqCounter;
  const turnId = typeof frame.turn_id === "string" ? frame.turn_id : null;
  const id =
    (typeof frame.id === "string" ? frame.id : undefined) ??
    `${type}-${seq}`;
  const item = frame.item as Record<string, unknown> | undefined;
  const itemType = typeof item?.type === "string" ? item.type : "";

  const base = { id, seq, ts, turnId };

  switch (type) {
    case "sbx.session_meta":
      return { ...base, kind: "info", text: "session_meta" };
    case "sbx.turn_started":
    case "turn.started":
      return { ...base, kind: "status", status: "running" };
    case "sbx.turn_finished":
    case "turn.completed":
      return { ...base, kind: "status", status: "finished" };
    case "turn.failed":
    case "sbx.error":
    case "error":
      return {
        ...base,
        kind: "error",
        error:
          toTurnError(frame.error) ??
          ({
            code: "runtime_error",
            message:
              typeof frame.message === "string" ? frame.message : "Turn failed",
            retryable: false,
          } satisfies TurnError),
      };
    case "item.started":
    case "item.updated":
    case "item.completed":
      if (!item) return null;
      switch (itemType) {
        case "agent_message":
          return {
            ...base,
            kind: "message",
            role: "assistant",
            text: typeof item.text === "string" ? item.text : "",
          };
        case "reasoning":
          return {
            ...base,
            kind: "reasoning",
            text: typeof item.text === "string" ? item.text : "",
          };
        case "command_execution": {
          const cmd = item.command ?? item.cmd;
          return {
            ...base,
            kind: "command",
            command: typeof cmd === "string" ? cmd : "",
            exitCode:
              typeof item.exit_code === "number" ? item.exit_code : undefined,
            output:
              typeof item.output === "string"
                ? item.output
                : typeof item.aggregated_output === "string"
                  ? item.aggregated_output
                  : undefined,
          };
        }
        case "file_change":
          return {
            ...base,
            kind: "file_change",
            path: typeof item.path === "string" ? item.path : "",
            changeType:
              item.change_type === "added" ||
              item.change_type === "modified" ||
              item.change_type === "deleted"
                ? item.change_type
                : "modified",
          };
        case "error":
          return {
            ...base,
            kind: "error",
            error:
              toTurnError(item.error) ??
              ({
                code: "runtime_error",
                message:
                  typeof item.message === "string"
                    ? item.message
                    : "Item failed",
                retryable: false,
              } satisfies TurnError),
          };
        default:
          return { ...base, kind: "info", text: itemType || "item" };
      }
    default:
      // status frames from V2 lifecycle (queued/starting/running)
      if (type === "session.status" || type === "status") {
        const status = typeof frame.status === "string" ? frame.status : "";
        return status ? { ...base, kind: "status", status } : null;
      }
      return null;
  }
}

import type {
  ActivityItem,
  ErrorKind,
  SessionEndReason,
  SessionPhase,
  TurnError,
  TurnStatus,
} from "./types";

/* eslint-disable @typescript-eslint/no-explicit-any */

/**
 * Wire → product mapping. All V2 projection shapes (SessionView / RunView /
 * RevisionView / SSE frames) are translated here so nothing else needs to
 * know backend vocabulary.
 */

/**
 * Product phase from the wire ``phase`` (authoritative) or ``status``
 * (fallback). V2 phases: provisioning|queued|running|delivering|finished|
 * failed|cancelled. Unknown values never claim "queued" — they fall back to
 * the status map, then a neutral "idle" (finished-but-silent is the least
 * misleading label).
 */
export function normalizePhase(phase?: string | null, status?: string | null): SessionPhase {
  const p = String(phase ?? "").toLowerCase();
  switch (p) {
    case "provisioning":
      return "starting";
    case "queued":
      return "queued";
    case "running":
    case "delivering":
      return "running";
    case "finished":
      return "idle";
    case "cancelled":
      return "ended";
    case "failed":
      return "failed";
  }
  const s = String(status ?? "").toLowerCase();
  switch (s) {
    case "queued":
      return "queued";
    case "running":
      return "running";
    case "finished":
      return "idle";
    case "cancelled":
      return "ended";
    case "failed":
      return "failed";
  }
  return "idle";
}

export function endReasonOf(phase?: string | null, status?: string | null): SessionEndReason {
  const p = String(phase ?? status ?? "").toLowerCase();
  if (p === "cancelled") return "cancelled";
  if (p === "failed") return "failed";
  return null;
}

export function normalizeTurnStatus(s: string | null | undefined): TurnStatus {
  switch (String(s ?? "").toLowerCase()) {
    case "queued":
    case "creating":
      return "queued";
    case "running":
      return "running";
    case "finished":
    case "succeeded":
      return "finished";
    case "failed":
    case "error":
    case "expired":
      return "failed";
    case "cancelled":
      return "cancelled";
    default:
      return "queued";
  }
}

/** Map a canonical error code (error_catalog + run error codes) onto a
 * product-level action kind. */
export function toErrorKind(subcode: string, httpStatus = 0): ErrorKind {
  switch (subcode) {
    // provider account needs (re)login or credential repair
    case "auth_invalid":
    case "account_unverified":
    case "schema_mismatch":
    case "credential_expired":
    case "token_expired":
    case "reauth_required":
    case "connect_failed":
      return "provider_login";
    // scheduler/busy states — actionable retry
    case "account_busy":
    case "account_unavailable":
    case "provider_exhausted":
    case "concurrency_limit":
    case "model_unavailable":
    case "rate_limited":
      return "provider_busy";
    // runtime cannot run right now
    case "unavailable":
    case "runtime_disabled":
    case "workspace_unavailable":
    case "provider_down":
      return "runtime_disabled";
    case "github_app_unconfigured":
    case "github_app_upstream":
      return "github_required";
    case "session_not_runnable":
    case "task_not_retryable":
    case "delivery_failed":
    case "checkout_failed":
    case "repo_unavailable":
    case "sandbox_lost":
    case "timeout":
      return "session_failed";
    case "unauthorized":
    case "forbidden":
    case "grant_invalid":
    case "pair_invalid":
    case "github_app_state":
      return "unauthorized";
    case "not_found":
    case "session_not_found":
    case "revision_not_found":
      return "not_found";
    case "turn_in_progress":
    case "task_active":
    case "session_active":
    case "idempotency_conflict":
    case "idempotency_in_progress":
      return "conflict";
    default:
      break;
  }
  if (httpStatus === 0) return "network";
  if (httpStatus === 401 || httpStatus === 403) return "unauthorized";
  if (httpStatus === 404) return "not_found";
  if (httpStatus === 409) return "conflict";
  return httpStatus >= 500 ? "runtime_disabled" : "unknown";
}

export function toTurnError(raw: any): TurnError | null {
  if (!raw) return null;
  if (typeof raw === "string") {
    return { code: "error", message: raw, retryable: false };
  }
  return {
    code: String(raw.code ?? "error"),
    source: raw.source ?? undefined,
    message: String(raw.message ?? "unknown error"),
    retryable: Boolean(raw.retryable),
    retryAfter:
      typeof raw.retry_after === "number"
        ? raw.retry_after
        : typeof raw.retryAfter === "number"
          ? raw.retryAfter
          : undefined,
  };
}

/** Stable per-session sequence for ActivityItem ordering across replays. */
export function nextSeq(): number {
  seq += 1;
  return seq;
}
let seq = 0;

const KIND_MAP: Record<string, string> = {
  added: "added",
  modified: "modified",
  updated: "modified",
  edited: "modified",
  deleted: "deleted",
  removed: "deleted",
};

function normalizeChangeKind(kind: unknown): "added" | "modified" | "deleted" {
  return (KIND_MAP[String(kind ?? "").toLowerCase()] ?? "modified") as
    | "added"
    | "modified"
    | "deleted";
}

/**
 * Normalize one V2 SSE frame into an ActivityItem, or null when the frame
 * is session-level (handled separately by the subscriber). Handles the
 * canonical item.* vocabulary plus turn.status frames.
 *
 * item frames share a stable id ``item-<item.id>`` so the page replaces the
 * started placeholder when the completed frame arrives (no duplicate rows).
 */
export function normalizeEvent(frame: any, sessionId: string): ActivityItem | null {
  if (!frame || typeof frame !== "object") return null;
  const type = String(frame.type ?? "");
  const n: number | undefined =
    typeof frame.n === "number" ? frame.n : undefined;
  const turnId = n != null ? `turn-${n}` : null;
  const ts = new Date().toISOString();

  // ---------- canonical item.* frames ----------
  if (type === "item.started" || type === "item.updated" || type === "item.completed") {
    const item = frame.item ?? {};
    const itemId = item.id != null ? `item-${item.id}` : `evt-${sessionId}-${nextSeq()}`;
    const itemStatus = String(item.status ?? "").toLowerCase();
    const inFlight = type !== "item.completed" && itemStatus !== "completed";

    switch (String(item.type ?? "")) {
      case "agent_message":
        // completed only — assistant text appended to the conversation.
        return {
          id: itemId, seq: nextSeq(), ts, turnId, n,
          kind: "message", role: "assistant",
          text: String(item.text ?? ""),
          status: inFlight ? "running" : "finished",
        };
      case "reasoning":
        return {
          id: itemId, seq: nextSeq(), ts, turnId, n,
          kind: "reasoning", role: "assistant",
          text: String(item.text ?? ""),
          status: inFlight ? "running" : "finished",
        };
      case "command_execution":
        return {
          id: itemId, seq: nextSeq(), ts, turnId, n,
          kind: "command",
          command: String(item.command ?? ""),
          output: item.aggregated_output != null ? String(item.aggregated_output) : undefined,
          exitCode: typeof item.exit_code === "number" ? item.exit_code : undefined,
          status: inFlight ? "running" : itemStatus === "failed" ? "failed" : "finished",
        };
      case "file_change": {
        // Canonical shape: changes:[{path,kind}] — a list, not a lone path.
        const changes = Array.isArray(item.changes)
          ? item.changes.map((c: any) => ({
              path: String(c?.path ?? ""),
              kind: String(c?.kind ?? "modified"),
            })).filter((c: { path: string }) => c.path)
          : [];
        const first = changes[0];
        return {
          id: itemId, seq: nextSeq(), ts, turnId, n,
          kind: "file_change",
          path: first?.path,
          changeType: first ? normalizeChangeKind(first.kind) : undefined,
          changes,
          status: inFlight ? "running" : "finished",
        };
      }
      case "error":
        return {
          id: itemId, seq: nextSeq(), ts, turnId, n,
          kind: "error",
          text: String(item.message ?? "error"),
          status: "failed",
        };
      default:
        // Unknown item types pass through as info — forward-compatible.
        return {
          id: itemId, seq: nextSeq(), ts, turnId, n,
          kind: "info",
          text: String(item.text ?? item.message ?? item.type ?? ""),
          status: inFlight ? "running" : "finished",
        };
    }
  }

  // ---------- turn-level frames ----------
  if (type === "turn.started") {
    return {
      id: `evt-status-${sessionId}-turnstart-${n ?? nextSeq()}`,
      seq: nextSeq(), ts, turnId, n,
      kind: "status", status: "running",
      text: `turn ${n ?? ""} started`.trim(),
    };
  }
  if (type === "turn.finished") {
    const ok = String(frame.status ?? "finished") === "finished";
    return {
      id: `evt-status-${sessionId}-turnfin-${n ?? nextSeq()}`,
      seq: nextSeq(), ts, turnId, n,
      kind: "status",
      status: ok ? "finished" : "failed",
      text: `turn ${n ?? ""} ${ok ? "finished" : "failed"}`.trim(),
    };
  }
  if (type === "turn.failed") {
    const err = toTurnError(frame.error);
    return {
      id: `evt-status-${sessionId}-turnfail-${n ?? nextSeq()}`,
      seq: nextSeq(), ts, turnId, n,
      kind: "error",
      text: err?.message ?? String(frame.message ?? "turn failed"),
      error: err ?? undefined,
      status: "failed",
    };
  }
  if (type === "turn.completed") {
    return {
      id: `evt-status-${sessionId}-turncomp-${n ?? nextSeq()}`,
      seq: nextSeq(), ts, turnId, n,
      kind: "status", status: "finished",
      text: `turn ${n ?? ""} completed`.trim(),
    };
  }
  if (type === "error") {
    return {
      id: `evt-err-${sessionId}-${nextSeq()}`,
      seq: nextSeq(), ts, turnId, n,
      kind: "error",
      text: String(frame.message ?? "error"),
      status: "failed",
    };
  }

  // session.status / session.meta / unknown → not an ActivityItem.
  return null;
}

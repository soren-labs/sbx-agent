import { describe, expect, it } from "vitest";
import {
  endReasonOf,
  normalizeEvent,
  normalizePhase,
  normalizeTurnStatus,
  toErrorKind,
  toTurnError,
} from "../api/normalize";

describe("normalizePhase", () => {
  it("maps backend statuses to product phases", () => {
    expect(normalizePhase("queued")).toBe("queued");
    expect(normalizePhase("creating")).toBe("starting");
    expect(normalizePhase("running")).toBe("running");
    expect(normalizePhase("idle")).toBe("idle");
    expect(normalizePhase("suspended")).toBe("idle");
    expect(normalizePhase("closed")).toBe("ended");
    expect(normalizePhase("timed_out")).toBe("ended");
    expect(normalizePhase("lost")).toBe("ended");
    expect(normalizePhase("failed")).toBe("failed");
  });
  it("treats unknown/empty as queued", () => {
    expect(normalizePhase("???")).toBe("queued");
    expect(normalizePhase(null)).toBe("queued");
    expect(normalizePhase(undefined)).toBe("queued");
  });
});

describe("endReasonOf", () => {
  it("extracts terminal reasons", () => {
    expect(endReasonOf("closed")).toBe("closed");
    expect(endReasonOf("timed_out")).toBe("timed_out");
    expect(endReasonOf("lost")).toBe("lost");
    expect(endReasonOf("running")).toBeNull();
  });
});

describe("normalizeTurnStatus", () => {
  it("maps cursor-shaped run states", () => {
    expect(normalizeTurnStatus("CREATING")).toBe("queued");
    expect(normalizeTurnStatus("RUNNING")).toBe("running");
    expect(normalizeTurnStatus("FINISHED")).toBe("finished");
    expect(normalizeTurnStatus("ERROR")).toBe("error");
    expect(normalizeTurnStatus("CANCELLED")).toBe("cancelled");
    expect(normalizeTurnStatus("EXPIRED")).toBe("cancelled");
    expect(normalizeTurnStatus("UNKNOWN")).toBe("error");
  });
});

describe("toErrorKind", () => {
  it("maps login problems", () => {
    expect(toErrorKind("auth_invalid")).toBe("provider_login");
    expect(toErrorKind("account_unverified")).toBe("provider_login");
    expect(toErrorKind("connect_failed")).toBe("provider_login");
  });
  it("maps busy/capacity problems", () => {
    expect(toErrorKind("account_busy")).toBe("provider_busy");
    expect(toErrorKind("provider_exhausted")).toBe("provider_busy");
    expect(toErrorKind("concurrency_limit")).toBe("provider_busy");
    expect(toErrorKind("rate_limited")).toBe("provider_busy");
    expect(toErrorKind("model_capacity")).toBe("provider_busy");
  });
  it("maps runtime/github/session problems", () => {
    expect(toErrorKind("runtime_disabled")).toBe("runtime_disabled");
    expect(toErrorKind("runtime_error")).toBe("runtime_disabled");
    expect(toErrorKind("github_app_unconfigured")).toBe("github_required");
    expect(toErrorKind("session_not_runnable")).toBe("session_failed");
  });
  it("maps generic http statuses", () => {
    expect(toErrorKind("unauthorized")).toBe("unauthorized");
    expect(toErrorKind("not_found")).toBe("not_found");
    expect(toErrorKind("turn_in_progress")).toBe("conflict");
    expect(toErrorKind("anything", 401)).toBe("unauthorized");
    expect(toErrorKind("anything", 404)).toBe("not_found");
    expect(toErrorKind("anything", 503)).toBe("network");
    expect(toErrorKind("anything", 400)).toBe("unknown");
  });
});

describe("normalizeEvent", () => {
  it("normalizes canonical item events", () => {
    const msg = normalizeEvent({
      type: "item.completed",
      turn_id: "t1",
      item: { type: "agent_message", text: "hello" },
    });
    expect(msg).toMatchObject({ kind: "message", role: "assistant", text: "hello" });

    const cmd = normalizeEvent({
      type: "item.completed",
      item: { type: "command_execution", command: "ls", exit_code: 0 },
    });
    expect(cmd).toMatchObject({ kind: "command", command: "ls", exitCode: 0 });

    const fc = normalizeEvent({
      type: "item.completed",
      item: { type: "file_change", path: "a.ts", change_type: "added" },
    });
    expect(fc).toMatchObject({ kind: "file_change", path: "a.ts", changeType: "added" });
  });

  it("normalizes lifecycle events", () => {
    expect(normalizeEvent({ type: "turn.started" })).toMatchObject({
      kind: "status",
      status: "running",
    });
    expect(normalizeEvent({ type: "turn.completed" })).toMatchObject({
      kind: "status",
      status: "finished",
    });
    const failed = normalizeEvent({
      type: "turn.failed",
      error: { code: "timeout", message: "hit limit", retryable: true },
    });
    expect(failed).toMatchObject({ kind: "error" });
    expect((failed as { error?: { code: string } }).error?.code).toBe("timeout");
  });

  it("drops keepalives and unknown frames", () => {
    expect(normalizeEvent(null)).toBeNull();
    expect(normalizeEvent({ type: "mystery" })).toBeNull();
    expect(normalizeEvent("keepalive")).toBeNull();
  });
});

describe("toTurnError", () => {
  it("parses canonical run error", () => {
    expect(
      toTurnError({
        code: "rate_limited",
        source: "provider",
        message: "429",
        retryable: true,
        retry_after: 12,
      }),
    ).toEqual({
      code: "rate_limited",
      source: "provider",
      message: "429",
      retryable: true,
      retryAfter: 12,
    });
    expect(toTurnError(null)).toBeNull();
    expect(toTurnError("x")).toBeNull();
  });
});

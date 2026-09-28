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
  it("maps V2 phases to product phases", () => {
    expect(normalizePhase("provisioning")).toBe("starting");
    expect(normalizePhase("queued")).toBe("queued");
    expect(normalizePhase("running")).toBe("running");
    expect(normalizePhase("delivering")).toBe("running");
    expect(normalizePhase("finished")).toBe("idle");
    expect(normalizePhase("cancelled")).toBe("ended");
    expect(normalizePhase("failed")).toBe("failed");
  });
  it("falls back to status when phase is unknown", () => {
    expect(normalizePhase("???", "running")).toBe("running");
    expect(normalizePhase("???", "finished")).toBe("idle");
    expect(normalizePhase("???", "cancelled")).toBe("ended");
  });
  it("never mislabels unknown as queued — neutral idle fallback", () => {
    expect(normalizePhase("???")).toBe("idle");
    expect(normalizePhase(null, "bogus")).toBe("idle");
    expect(normalizePhase(undefined, undefined)).toBe("idle");
  });
});

describe("endReasonOf", () => {
  it("extracts terminal reasons", () => {
    expect(endReasonOf("cancelled")).toBe("cancelled");
    expect(endReasonOf("failed")).toBe("failed");
    expect(endReasonOf("running")).toBeNull();
    expect(endReasonOf("finished")).toBeNull();
    // status fallback when phase absent
    expect(endReasonOf(undefined, "cancelled")).toBe("cancelled");
  });
});

describe("normalizeTurnStatus", () => {
  it("maps wire run statuses", () => {
    expect(normalizeTurnStatus("queued")).toBe("queued");
    expect(normalizeTurnStatus("RUNNING")).toBe("running");
    expect(normalizeTurnStatus("finished")).toBe("finished");
    expect(normalizeTurnStatus("failed")).toBe("failed");
    expect(normalizeTurnStatus("ERROR")).toBe("failed");
    expect(normalizeTurnStatus("EXPIRED")).toBe("failed");
    expect(normalizeTurnStatus("cancelled")).toBe("cancelled");
    expect(normalizeTurnStatus("UNKNOWN")).toBe("queued");
  });
});

describe("toErrorKind", () => {
  it("maps login problems", () => {
    expect(toErrorKind("auth_invalid")).toBe("provider_login");
    expect(toErrorKind("account_unverified")).toBe("provider_login");
    expect(toErrorKind("connect_failed")).toBe("provider_login");
  });
  it("maps busy/capacity problems (real scheduler codes)", () => {
    expect(toErrorKind("account_busy")).toBe("provider_busy");
    expect(toErrorKind("account_unavailable")).toBe("provider_busy");
    expect(toErrorKind("provider_exhausted")).toBe("provider_busy");
    expect(toErrorKind("concurrency_limit")).toBe("provider_busy");
  });
  it("maps runtime/github/session problems", () => {
    expect(toErrorKind("runtime_disabled")).toBe("runtime_disabled");
    expect(toErrorKind("unavailable")).toBe("runtime_disabled");
    expect(toErrorKind("workspace_unavailable")).toBe("runtime_disabled");
    expect(toErrorKind("github_app_unconfigured")).toBe("github_required");
    expect(toErrorKind("session_not_runnable")).toBe("session_failed");
    expect(toErrorKind("task_not_retryable")).toBe("session_failed");
    expect(toErrorKind("delivery_failed")).toBe("session_failed");
  });
  it("maps generic http statuses", () => {
    expect(toErrorKind("unauthorized")).toBe("unauthorized");
    expect(toErrorKind("forbidden")).toBe("unauthorized");
    expect(toErrorKind("not_found")).toBe("not_found");
    expect(toErrorKind("turn_in_progress")).toBe("conflict");
    expect(toErrorKind("idempotency_conflict")).toBe("conflict");
    expect(toErrorKind("anything", 401)).toBe("unauthorized");
    expect(toErrorKind("anything", 404)).toBe("not_found");
    expect(toErrorKind("anything", 503)).toBe("runtime_disabled");
    expect(toErrorKind("anything", 400)).toBe("unknown");
    expect(toErrorKind("anything", 0)).toBe("network");
  });
});

describe("normalizeEvent (V2 frames)", () => {
  const SID = "sess-test";

  it("normalizes canonical item.completed events", () => {
    const msg = normalizeEvent(
      {
        type: "item.completed",
        n: 2,
        item: { id: "i1", type: "agent_message", text: "hello" },
      },
      SID,
    );
    expect(msg).toMatchObject({
      kind: "message",
      role: "assistant",
      text: "hello",
      turnId: "turn-2",
    });

    const cmd = normalizeEvent(
      {
        type: "item.completed",
        n: 1,
        item: {
          id: "i2",
          type: "command_execution",
          command: "ls",
          aggregated_output: "x",
          exit_code: 0,
        },
      },
      SID,
    );
    expect(cmd).toMatchObject({ kind: "command", command: "ls", exitCode: 0, output: "x" });
  });

  it("file_change reads the canonical changes[] list", () => {
    const fc = normalizeEvent(
      {
        type: "item.completed",
        n: 1,
        item: {
          id: "fc1",
          type: "file_change",
          changes: [
            { path: "a.ts", kind: "added" },
            { path: "b.ts", kind: "deleted" },
          ],
        },
      },
      SID,
    );
    expect(fc).toMatchObject({
      kind: "file_change",
      path: "a.ts",
      changeType: "added",
      changes: [
        { path: "a.ts", kind: "added" },
        { path: "b.ts", kind: "deleted" },
      ],
    });
  });

  it("item.started and item.completed share a stable id (no dupes)", () => {
    const started = normalizeEvent(
      {
        type: "item.started",
        n: 1,
        item: { id: "cmd-9", type: "command_execution", command: "make", status: "in_progress" },
      },
      SID,
    );
    const completed = normalizeEvent(
      {
        type: "item.completed",
        n: 1,
        item: { id: "cmd-9", type: "command_execution", command: "make", exit_code: 0, status: "completed" },
      },
      SID,
    );
    expect(started?.id).toBe("item-cmd-9");
    expect(completed?.id).toBe("item-cmd-9");
    expect(started?.status).toBe("running");
    expect(completed?.status).toBe("finished");
  });

  it("normalizes turn lifecycle frames", () => {
    expect(normalizeEvent({ type: "turn.started", n: 3 }, SID)).toMatchObject({
      kind: "status",
      status: "running",
      turnId: "turn-3",
    });
    expect(
      normalizeEvent({ type: "turn.finished", n: 3, status: "finished" }, SID),
    ).toMatchObject({ kind: "status", status: "finished" });
    const failed = normalizeEvent(
      { type: "turn.failed", n: 3, error: "hit limit" },
      SID,
    );
    expect(failed).toMatchObject({ kind: "error", status: "failed" });
    const top = normalizeEvent({ type: "error", message: "boom", n: 1 }, SID);
    expect(top).toMatchObject({ kind: "error", text: "boom" });
  });

  it("drops session frames and unknown types (handled by subscriber)", () => {
    expect(normalizeEvent(null, SID)).toBeNull();
    expect(normalizeEvent({ type: "session.status", phase: "running" }, SID)).toBeNull();
    expect(normalizeEvent({ type: "session.meta", provider: "codex" }, SID)).toBeNull();
    expect(normalizeEvent({ type: "mystery" }, SID)).toBeNull();
    expect(normalizeEvent("keepalive", SID)).toBeNull();
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
    expect(toTurnError("x")).toMatchObject({ code: "error", message: "x" });
  });
});

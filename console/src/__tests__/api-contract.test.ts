import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { FixtureSessionApi } from "../api/mock";
import { PROVIDERS, SESSIONS } from "../api/fixtures";
import type { Session } from "../api/types";

const api = () => {
  const a = new FixtureSessionApi();
  a.latencyMs = 0;
  a.autoAdvance = false;
  return a;
};

describe("FixtureSessionApi contract", () => {
  it("lists sessions with contract-required fields", async () => {
    const a = api();
    const list = await a.listSessions();
    expect(list.length).toBe(SESSIONS.length);
    for (const s of list) {
      expect(s).toMatchObject({
        id: expect.any(String),
        title: expect.any(String),
        phase: expect.any(String),
        provider: expect.any(String),
        model: expect.any(String),
        createdAt: expect.any(String),
        updatedAt: expect.any(String),
        turns: expect.any(Array),
      });
      // No internal ids leak into the product surface
      expect(JSON.stringify(s)).not.toMatch(/modal|lru|sandbox-[a-z0-9]{10,}/i);
    }
  });

  it("session phases stay inside the product enum", async () => {
    const a = api();
    const phases = new Set(["queued", "starting", "running", "idle", "ended", "failed"]);
    for (const s of await a.listSessions()) {
      expect(phases.has(s.phase)).toBe(true);
    }
  });

  it("createSession returns an optimistic shell in queued phase", async () => {
    const a = api();
    const s = await a.createSession({ prompt: "do a thing", repo: "owner/repo" });
    expect(s.phase).toBe("queued");
    expect(s.turns[0]).toMatchObject({ prompt: "do a thing", status: "queued" });
    expect(s.repo?.name).toBe("owner/repo");
    expect(s.title).toBe("do a thing");
  });

  it("createSession accepts Auto provider/model and resolves them", async () => {
    const a = api();
    const s = await a.createSession({ prompt: "x", provider: "auto", model: "auto" });
    expect(["codex", "antigravity", "grok", "opencode", "devin"]).toContain(s.provider);
    expect(s.model).toBeTruthy();
  });

  it("advances queued → starting → running via subscribe", async () => {
    const a = new FixtureSessionApi();
    a.latencyMs = 0;
    const s = await a.createSession({ prompt: "go" });
    const phases: string[] = [];
    a.subscribe(s.id, { onPhase: (p) => phases.push(p) });
    expect(phases[0]).toBe("queued"); // replay
    await vi.waitFor(
      () => {
        expect(phases).toContain("starting");
        expect(phases).toContain("running");
      },
      { timeout: 3000 },
    );
  });

  it("sendFollowUp streams normalized activity and finishes idle", async () => {
    const a = new FixtureSessionApi();
    a.latencyMs = 0;
    const s = SESSIONS.find((x) => x.phase === "idle")!;
    const items: string[] = [];
    a.subscribe(s.id, { onActivity: (i) => items.push(i.kind) });
    const { turnId } = await a.sendFollowUp(s.id, "also update the docs");
    expect(turnId).toBeTruthy();
    await vi.waitFor(
      async () => {
        const after = await a.getSession(s.id);
        expect(after.phase).toBe("idle");
        const last = after.turns[after.turns.length - 1];
        expect(last.status).toBe("finished");
      },
      { timeout: 4000 },
    );
    expect(items).toContain("command");
    expect(items).toContain("message");
  });

  it("sendFollowUp on ended session raises session_failed", async () => {
    const a = api();
    const ended = SESSIONS.find((s) => s.phase === "ended")!;
    await expect(a.sendFollowUp(ended.id, "hi")).rejects.toMatchObject({
      kind: "session_failed",
    });
  });

  it("scenario errors produce ApiError with mapped kinds", async () => {
    for (const [scenario, kind] of [
      ["provider_login", "provider_login"],
      ["provider_busy", "provider_busy"],
      ["runtime_disabled", "runtime_disabled"],
      ["github_required", "github_required"],
      ["create_fails", "session_failed"],
    ] as const) {
      const a = api();
      a.scenario = scenario;
      await expect(
        a.createSession({ prompt: "x" }),
        scenario,
      ).rejects.toMatchObject({ kind });
    }
  });

  it("getSession 404 maps to not_found", async () => {
    const a = api();
    await expect(a.getSession("nope")).rejects.toMatchObject({
      kind: "not_found",
      httpStatus: 404,
    });
    await expect(a.getSession("nope")).rejects.toBeInstanceOf(ApiError);
  });

  it("providers and integrations come from fixtures", async () => {
    const a = api();
    const providers = await a.listProviders();
    expect(providers.map((p) => p.id)).toEqual(PROVIDERS.map((p) => p.id));
    const integ = await a.getIntegrations();
    expect(integ.github.connected).toBe(true);
    expect(integ.runtime.enabled).toBe(true);
  });

  it("stream_drops scenario emits disconnect then reconnect", async () => {
    const a = api();
    a.scenario = "stream_drops";
    const events: string[] = [];
    a.subscribe(SESSIONS[0].id, {
      onDisconnect: () => events.push("down"),
      onReconnect: () => events.push("up"),
    });
    await vi.waitFor(() => expect(events).toEqual(["down", "up"]), {
      timeout: 4000,
    });
  });

  it("stopSession cancels the active turn", async () => {
    const a = api();
    const running = SESSIONS.find((s) => s.phase === "running")!;
    await a.stopSession(running.id);
    const after = await a.getSession(running.id);
    expect(after.phase).toBe("idle");
    expect(after.turns[0].status).toBe("cancelled");
  });

  it("closeSession ends the session", async () => {
    const a = api();
    const s = await a.createSession({ prompt: "x" });
    const closed = await a.closeSession(s.id);
    expect(closed.phase).toBe("ended");
    expect(closed.endReason).toBe("closed");
  });
});

describe("fixture data sanity", () => {
  it("fixtures cover the lifecycle phases", () => {
    const phases = new Set(SESSIONS.map((s: Session) => s.phase));
    for (const p of ["queued", "running", "idle", "ended", "failed"]) {
      expect(phases.has(p as Session["phase"])).toBe(true);
    }
  });
});

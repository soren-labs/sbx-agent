import { describe, expect, it, vi, afterEach } from "vitest";
import { ApiError } from "../api/client";
import { HttpSessionApi, toCreateSessionRequest } from "../api/http";
import { FixtureSessionApi } from "../api/mock";
import { MODELS, PROVIDERS, SESSIONS } from "../api/fixtures";
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
        status: expect.any(String),
        phase: expect.any(String),
        createdAt: expect.any(String),
        updatedAt: expect.any(String),
        turns: expect.any(Array),
        turnCount: expect.any(Number),
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
    expect(s.status).toBe("queued");
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

  it("sendFollowUp returns {session, n} and streams normalized activity", async () => {
    const a = new FixtureSessionApi();
    a.latencyMs = 0;
    const s = SESSIONS.find((x) => x.phase === "idle")!;
    const items: string[] = [];
    a.subscribe(s.id, { onActivity: (i) => items.push(i.kind) });
    const { session, n } = await a.sendFollowUp(s.id, "also update the docs");
    expect(n).toBe(s.turnCount + 1);
    expect(session.id).toBe(s.id);
    await vi.waitFor(
      async () => {
        const after = await a.getSession(s.id);
        expect(after.phase).toBe("idle");
        const last = after.turns[after.turns.length - 1];
        expect(last.id).toBe(`turn-${n}`);
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

  it("providers, models and integrations come from fixtures", async () => {
    const a = api();
    const providers = await a.listProviders();
    expect(providers.map((p) => p.id)).toEqual(PROVIDERS.map((p) => p.id));
    const models = await a.listModels();
    expect(models.map((m) => m.model)).toEqual(MODELS.map((m) => m.model));
    const integ = await a.getIntegrations();
    expect(integ.github.connected).toBe(true);
    expect(integ.github.accounts).toContain("soren-labs");
    expect(integ.runtime.enabled).toBe(true);
  });

  it("beginGithubAuthorize returns an install url", async () => {
    const a = api();
    const { url } = await a.beginGithubAuthorize();
    expect(url).toMatch(/^https:\/\/github\.com\//);
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

  it("stopSession cancels the active turn and ends the session", async () => {
    const a = api();
    const running = SESSIONS.find((s) => s.phase === "running")!;
    const stopped = await a.stopSession(running.id);
    expect(stopped.status).toBe("cancelled");
    const after = await a.getSession(running.id);
    expect(after.phase).toBe("ended");
    expect(after.turns[0].status).toBe("cancelled");
  });

  it("retrySession on a failed session queues a new turn", async () => {
    const a = api();
    const failed = SESSIONS.find((s) => s.phase === "failed")!;
    const retried = await a.retrySession(failed.id);
    expect(retried.phase).toBe("queued");
    expect(retried.turns.at(-1)?.prompt).toBe(failed.prompt);
  });

  it("listChanges returns revision rows for the delivered fixture", async () => {
    const a = api();
    const s = SESSIONS.find((x) => x.hasChanges)!;
    const changes = await a.listChanges(s.id);
    expect(changes.length).toBeGreaterThan(0);
    const rev = changes.find((c) => c.kind === "revision");
    expect(rev?.url).toMatch(/pull\/97/);
  });
});

describe("fixture data sanity", () => {
  it("fixtures cover the lifecycle phases", () => {
    const phases = new Set(SESSIONS.map((s: Session) => s.phase));
    for (const p of ["queued", "running", "idle", "ended", "failed"]) {
      expect(phases.has(p as Session["phase"])).toBe(true);
    }
  });

  it("fixture turn ids follow the turn-<n> convention", () => {
    for (const s of SESSIONS) {
      s.turns.forEach((tn, i) => {
        expect(tn.id).toBe(`turn-${i + 1}`);
        expect(tn.index).toBe(i + 1);
      });
    }
  });
});

/* ------------------------------------------------------------------ */
/* HttpSessionApi wire contract — regression coverage for the review    */
/* blockers. fetch is stubbed; we assert on real /v2 + /v1 shapes.      */
/* ------------------------------------------------------------------ */

const okJson = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

function stubFetch(handler: (path: string, init?: RequestInit) => Response) {
  const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof Request
          ? input.url
          : input.href;
    return handler(new URL(url).pathname + new URL(url).search, init);
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("HttpSessionApi — V2 wire shapes", () => {
  const VIEW = {
    id: "sess-0123456789abcdef",
    title: "hello",
    status: "queued",
    phase: "queued",
    prompt: "hello",
    created_at: "2026-09-28T00:00:00Z",
    updated_at: "2026-09-28T00:00:00Z",
    repository: { repo: "a/b", ref: "main" },
    execution: { provider: "auto", account_id: "auto", model: "auto", reasoning_effort: "auto" },
    delivery: null,
    turns: 0,
    usage: null,
    cost_estimate_usd: null,
    changes: null,
    error: null,
  };

  it("createSession POSTs CreateSessionRequest and reads {session}", async () => {
    const fetchSpy = stubFetch((path) => {
      expect(path).toBe("/v2/sessions");
      return okJson({ session: { ...VIEW, id: "sess-abc" } }, 201);
    });
    const api = new HttpSessionApi("https://cp.test");
    const s = await api.createSession({ prompt: "hi", repo: "a/b" });
    expect(s.id).toBe("sess-abc");
    const body = JSON.parse(String(fetchSpy.mock.calls[0][1]?.body));
    expect(body.prompt).toBe("hi");
    expect(body.repository).toEqual({ repo: "a/b" });
    // Auto stays literal — backend resolves; never a hardcoded provider.
    expect(body.execution).toMatchObject({
      provider: "auto",
      account_id: "auto",
      model: "auto",
      reasoning_effort: "auto",
    });
  });

  it("toCreateSessionRequest maps delivery/resources/compute/idle", () => {
    const req = toCreateSessionRequest({
      prompt: "x",
      provider: "grok",
      account: "grok-main",
      model: "grok-4",
      effort: "high",
      delivery: "draft_pr",
      deliveryTarget: "develop",
      secrets: ["AWS_KEY"],
      mcpServers: ["github"],
      compute: { cpu: 4, memoryMib: 8192 },
      idleTimeoutS: 900,
    });
    expect(req.execution).toMatchObject({
      provider: "grok",
      account_id: "grok-main",
      model: "grok-4",
      reasoning_effort: "high",
    });
    expect(req.delivery).toEqual({
      auto_publish: true,
      pull_request: { draft: true, target: "develop" },
    });
    expect(req.advanced).toEqual({
      resources: { secrets: ["AWS_KEY"], mcp: ["github"] },
      compute: { cpu: 4, memory_mib: 8192 },
      idle_timeout_s: 900,
    });
  });

  it("toCreateSessionRequest never emits workspace/agent envelopes", () => {
    const req = toCreateSessionRequest({ prompt: "x", repo: "a/b" });
    expect(req).not.toHaveProperty("workspace");
    expect(req).not.toHaveProperty("agent");
    expect(req).not.toHaveProperty("task");
  });

  it("listSessions reads the {sessions,total} envelope", async () => {
    stubFetch((path) => {
      expect(path).toBe("/v2/sessions?limit=100");
      return okJson({ sessions: [VIEW], total: 1, limit: 100, offset: 0 });
    });
    const api = new HttpSessionApi("https://cp.test");
    const list = await api.listSessions();
    expect(list[0].id).toBe("sess-0123456789abcdef");
    expect(list[0].phase).toBe("queued");
  });

  it("getSession reads {session, runs[]}", async () => {
    stubFetch(() =>
      okJson({
        session: { ...VIEW, status: "finished", phase: "finished", turns: 1 },
        runs: [
          {
            n: 1,
            status: "finished",
            prompt: "hello",
            result: "done",
            created_at: "2026-09-28T00:00:00Z",
          },
        ],
        run_count: 1,
        truncated: false,
      }),
    );
    const api = new HttpSessionApi("https://cp.test");
    const s = await api.getSession(VIEW.id);
    expect(s.phase).toBe("idle");
    expect(s.turns[0]).toMatchObject({ id: "turn-1", status: "finished", result: "done" });
  });

  it("sendFollowUp POSTs {prompt, on_busy} → reads {session, message.n}", async () => {
    const fetchSpy = stubFetch((path, init) => {
      expect(path).toBe(`/v2/sessions/${VIEW.id}/messages`);
      expect(String(init?.body)).toContain('"on_busy":"queue"');
      return okJson({ session: VIEW, message: { n: 2, status: "queued" } }, 202);
    });
    const api = new HttpSessionApi("https://cp.test");
    const { session, n } = await api.sendFollowUp(VIEW.id, "more");
    expect(n).toBe(2);
    expect(session.id).toBe(VIEW.id);
    expect(fetchSpy).toHaveBeenCalledOnce();
  });

  it("listProviders reads the real Provider row shape", async () => {
    stubFetch((path) => {
      expect(path).toBe("/v1/providers");
      return okJson({
        providers: [
          {
            provider: "codex",
            support: "full",
            status: "available",
            readiness: "needs_login",
            default_models: ["gpt-5-codex"],
            runtime: { enabled: true, status: "ready" },
            connection: {
              status: "connected",
              accounts_total: 2,
              accounts_available: 1,
            },
          },
        ],
      });
    });
    const api = new HttpSessionApi("https://cp.test");
    const [p] = await api.listProviders();
    expect(p).toMatchObject({
      id: "codex",
      readiness: "needs_login",
      models: ["gpt-5-codex"],
      runtimeStatus: "ready",
      connectionStatus: "connected",
      accountsTotal: 2,
      accountsAvailable: 1,
      needsLogin: true,
    });
  });

  it("getIntegrations reads GitHubAppStatus + authorize endpoint", async () => {
    stubFetch((path) => {
      if (path === "/v1/providers") return okJson({ providers: [] });
      if (path === "/v1/github/app") {
        return okJson({
          configured: true,
          installable: true,
          app_slug: "sbx-browser",
          source: "env",
          installations: [
            { installation_id: 1, account_login: "soren-labs", suspended: false },
          ],
        });
      }
      throw new Error(`unexpected ${path}`);
    });
    const api = new HttpSessionApi("https://cp.test");
    const integ = await api.getIntegrations();
    expect(integ.github).toMatchObject({
      configured: true,
      connected: true,
      accounts: ["soren-labs"],
      appSlug: "sbx-browser",
    });
  });

  it("beginGithubAuthorize returns POST /v1/github/app/authorize url", async () => {
    const fetchSpy = stubFetch((path, init) => {
      expect(path).toBe("/v1/github/app/authorize");
      expect(init?.method).toBe("POST");
      return okJson({ authorize_url: "https://github.com/apps/x", state: "s", expires_at: "e" }, 201);
    });
    const api = new HttpSessionApi("https://cp.test");
    const { url } = await api.beginGithubAuthorize();
    expect(url).toBe("https://github.com/apps/x");
    expect(fetchSpy).toHaveBeenCalledOnce();
  });

  it("listChanges maps ChangesView + RevisionView rows", async () => {
    stubFetch((path) => {
      expect(path).toBe(`/v2/sessions/${VIEW.id}/changes`);
      return okJson({
        session: VIEW,
        changes: {
          status: "ready",
          base_sha: "a".repeat(40),
          head_sha: "b".repeat(40),
          branch: "sbx/x-1",
          pull_request: { number: 5, url: "https://github.com/a/b/pull/5", state: "open" },
        },
        revisions: [
          {
            n: 1,
            status: "delivered",
            head_sha: "b".repeat(40),
            created_at: "2026-09-28T00:00:00Z",
            updated_at: "2026-09-28T00:01:00Z",
            delivery: {
              required: true,
              status: "delivered",
              branch: "sbx/x-1",
              pull_request: { number: 5, url: "https://github.com/a/b/pull/5", state: "open" },
            },
          },
        ],
      });
    });
    const api = new HttpSessionApi("https://cp.test");
    const rows = await api.listChanges(VIEW.id);
    expect(rows.map((r) => r.kind)).toEqual(["workspace", "revision"]);
    expect(rows[1]).toMatchObject({ n: 1, status: "delivered", url: "https://github.com/a/b/pull/5" });
  });

  it("listChangesDiff reads the parsed patch: file list, no bodies", async () => {
    stubFetch((path) => {
      expect(path).toBe(`/v2/sessions/${VIEW.id}/changes/diff`);
      return okJson({
        n: 1,
        base_sha: "a".repeat(40),
        head_sha: "b".repeat(40),
        files_changed: 2,
        additions: 7,
        deletions: 1,
        files: [
          { path: "a.ts", status: "modified", additions: 5, deletions: 1, old_path: null, diff: null },
          { path: "b.ts", status: "added", additions: 2, deletions: 0, old_path: null, diff: null },
        ],
      });
    });
    const api = new HttpSessionApi("https://cp.test");
    const diff = await api.listChangesDiff(VIEW.id);
    expect(diff).toMatchObject({
      n: 1,
      baseSha: "a".repeat(40),
      headSha: "b".repeat(40),
      filesChanged: 2,
      additions: 7,
      deletions: 1,
    });
    expect(diff.files[1]).toMatchObject({ path: "b.ts", status: "added" });
    // stat rows carry no bodies — a body only comes back via ?path=
    expect(diff.files.every((f) => f.diff == null)).toBe(true);
  });

  it("getFileDiff maps a 404 to a not_found ApiError", async () => {
    stubFetch((path) => {
      expect(path).toBe(
        `/v2/sessions/${VIEW.id}/changes/diff?path=missing.ts`,
      );
      return okJson(
        { error: { code: "not_found", message: "no diff for file missing.ts", retryable: false } },
        404,
      );
    });
    const api = new HttpSessionApi("https://cp.test");
    await expect(api.getFileDiff(VIEW.id, "missing.ts")).rejects.toMatchObject({
      kind: "not_found",
    });
  });

  it("deliverSession POSTs {pull_request} and reads {session, revision}", async () => {
    const fetchSpy = stubFetch((path, init) => {
      expect(path).toBe(`/v2/sessions/${VIEW.id}/deliver`);
      expect(init?.method).toBe("POST");
      return okJson({
        session: { ...VIEW, status: "finished", phase: "finished" },
        revision: {
          n: 1,
          status: "ready",
          head_sha: "b".repeat(40),
          created_at: "2026-09-28T00:00:00Z",
          updated_at: "2026-09-28T00:01:00Z",
          delivery: {
            required: true,
            status: "delivered",
            branch: "sbx/x-1",
            pull_request: {
              number: 5,
              url: "https://github.com/a/b/pull/5",
              state: "open",
            },
          },
        },
      });
    });
    const api = new HttpSessionApi("https://cp.test");
    const res = await api.deliverSession(VIEW.id, {
      title: "hello",
      draft: true,
    });
    const body = JSON.parse(String(fetchSpy.mock.calls[0][1]?.body));
    expect(body.pull_request).toEqual({ title: "hello", draft: true });
    expect(res.revision).toMatchObject({
      n: 1,
      deliveryStatus: "delivered",
      branch: "sbx/x-1",
      prNumber: 5,
      url: "https://github.com/a/b/pull/5",
    });
    expect(res.session.id).toBe(VIEW.id);
  });

  it("error envelope maps canonical codes to kinds", async () => {
    stubFetch(() =>
      okJson(
        { error: { code: "account_busy", message: "busy", retryable: true } },
        409,
      ),
    );
    const api = new HttpSessionApi("https://cp.test");
    await expect(api.getSession(VIEW.id)).rejects.toMatchObject({
      kind: "provider_busy",
      subcode: "account_busy",
      retryable: true,
    });
  });
});

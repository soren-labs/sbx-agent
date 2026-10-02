import { FixtureSessionApi } from "../api/mock";
import type { SessionEventHandlers } from "../api/client";
import type {
  NewSessionInput,
  DeliverInput,
  SessionDeliverResult,
  ActivityItem,
  Session,
  SessionChangesDiff,
  SessionFileDiff,
  ProviderInfo,
  ModelInfo,
} from "../api/types";
import { PROVIDERS } from "../api/fixtures";

const start = Date.now() - 8 * 60_000;
const ts = (seconds: number) => new Date(start + seconds * 1000).toISOString();
export const demoMode = import.meta.env.VITE_API_MODE === "mock";
export const providerNames: Record<string, string> = {
  codex: "Codex",
  devin: "Devin",
  antigravity: "Antigravity",
  grok: "Grok",
  opencode: "OpenCode",
};
export const demoModels: Record<string, string[]> = {
  codex: ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"],
  devin: ["swe-2"],
  antigravity: ["gemini-3.8-pro"],
  grok: ["grok-4.7"],
  opencode: ["muse-spark-1.3"],
};
const item = (seq: number, partial: Partial<ActivityItem>): ActivityItem => ({
  id: `demo-${seq}`,
  seq,
  ts: ts(seq * 12),
  turnId: "turn-1",
  kind: "info",
  ...partial,
});
export const demoActivity: ActivityItem[] = [
  item(1, {
    kind: "message",
    role: "assistant",
    text: "I’ll trace the session stream and reconnect flow, add a regression test, then check tests and the production build.",
  }),
  item(2, {
    kind: "reasoning",
    text: "Exploring the event stream and session lifecycle",
  }),
  item(3, {
    kind: "command",
    command: 'rg -n "EventSource|onReconnect|lastEventId" console/src',
    output:
      "console/src/api/http.ts:547  subscribe(sessionId, handlers)\nconsole/src/pages/SessionDetailPage.tsx:176  onReconnect",
    exitCode: 0,
  }),
  item(4, {
    kind: "command",
    command: "cat console/src/api/normalize.ts",
    output:
      "Read event normalization and stable item identifiers.\nFound: started and completed events share an item ID.",
    exitCode: 0,
  }),
  item(5, {
    kind: "message",
    role: "assistant",
    text: "Found the cause: replayed events were appended after reconnecting. I’m merging them by their stable event ID so the conversation keeps its order.",
  }),
  item(6, { kind: "reasoning", text: "Implementing stable event merging" }),
  item(7, {
    kind: "file_change",
    path: "console/src/state/session.ts",
    changes: [
      { path: "console/src/state/session.ts", kind: "added" },
      { path: "console/src/pages/SessionDetailPage.tsx", kind: "modified" },
    ],
  }),
  item(8, { kind: "file_change", path: "console/src/api/http.ts" }),
  item(9, {
    kind: "command",
    command: "npm run test -- session-stream",
    output:
      "✓ deduplicates replayed events\n✓ preserves activity order\n✓ restores the stream after disconnect\n✓ keeps follow-up messages\n\nTest Files  2 passed (2)\nTests       12 passed (12)\nDuration    1.42s",
    exitCode: 0,
  }),
  item(10, {
    kind: "message",
    role: "assistant",
    text: "The fix and regression tests are in place. All 12 tests pass. I’m checking the production build before handing off the changes.",
  }),
  item(11, {
    kind: "reasoning",
    text: "Checking the production build",
  }),
  item(12, {
    kind: "command",
    status: "running",
    command: "npm run build",
    output: "tsc --noEmit && vite build\nTransforming modules…",
  }),
];
function makeSession(
  id: string,
  title: string,
  phase: Session["phase"],
  age: number,
  provider = "codex",
): Session {
  const prompt =
    id === "stream-reconnect"
      ? "Fix duplicate messages when a session reconnects. Preserve the conversation order, add regression coverage, and open a draft PR when the checks pass."
      : title + ". Follow the existing conventions and verify the changes.";
  const finished = phase === "idle";
  return {
    id,
    title,
    phase,
    status: finished ? "finished" : phase === "failed" ? "failed" : "running",
    endReason: phase === "failed" ? "failed" : null,
    prompt,
    provider,
    model: demoModels[provider][0],
    effort: "high",
    accountLabel: "Soren · work",
    repo: { name: "soren-labs/sbx-browser", ref: "main" },
    compute: null,
    idleTimeoutS: 1800,
    delivery: finished
      ? {
          mode: "draft_pr",
          status: "delivered",
          branch: `sbx/${id}`,
          prNumber: 134,
          prState: "draft",
          prBase: "main",
        }
      : { mode: "draft_pr", status: "pending", branch: `sbx/${id}` },
    createdAt: new Date(Date.now() - age - 8 * 60_000).toISOString(),
    updatedAt: new Date(Date.now() - age).toISOString(),
    usage: { inputTokens: 28410, cachedInputTokens: 18320, outputTokens: 4230 },
    costUsd: null,
    turnCount: 1,
    hasChanges: phase !== "failed",
    error:
      phase === "failed"
        ? {
            code: "provider_auth_invalid",
            source: "provider",
            message:
              "Your OpenCode connection needs to be renewed. Reconnect the account, then retry this session.",
            retryable: true,
          }
        : null,
    lastActivityPreview: finished
      ? "Ready for review · all checks passed"
      : phase === "failed"
        ? "Account connection needs attention"
        : "Checking the production build",
    turns: [
      {
        id: "turn-1",
        index: 1,
        prompt,
        status: finished
          ? "finished"
          : phase === "failed"
            ? "failed"
            : "running",
        createdAt: ts(0),
        startedAt: ts(0),
        finishedAt: finished ? ts(440) : null,
        result: finished
          ? "Implemented the changes and verified the behavior. The draft pull request is ready for your review."
          : null,
        error: null,
        activity:
          phase === "failed"
            ? []
            : demoActivity.map((a) => ({
                ...a,
                ...(finished && a.seq === 12
                  ? {
                      exitCode: 0,
                      output:
                        "TypeScript passed. Vite production build completed.",
                    }
                  : {}),
                status:
                  finished && a.status === "running" ? "finished" : a.status,
              })),
      },
    ],
  };
}
export const demoSessions: Session[] = [
  makeSession("stream-reconnect", "Fix session stream reconnect", "running", 0),
  makeSession(
    "event-replay",
    "Deduplicate replayed session events",
    "idle",
    3_600_000,
    "devin",
  ),
  makeSession(
    "account-health",
    "Improve account health indicators",
    "failed",
    7_200_000,
    "opencode",
  ),
  makeSession(
    "empty-states",
    "Polish empty states and loading",
    "idle",
    86_400_000,
  ),
  makeSession(
    "provider-models",
    "Refresh provider model discovery",
    "idle",
    90_000_000,
    "grok",
  ),
  makeSession(
    "github-setup",
    "Simplify GitHub repository setup",
    "idle",
    172_800_000,
    "antigravity",
  ),
];
const patches: Record<string, string> = {
  "console/src/state/session.ts":
    '@@ -0,0 +1,15 @@\n+import type { ActivityItem } from "../api/types";\n+\n+/** Replayed events update the existing work item. */\n+export function mergeActivity(\n+  current: ActivityItem[],\n+  incoming: ActivityItem,\n+): ActivityItem[] {\n+  const items = new Map(current.map(item => [item.id, item]));\n+  items.set(incoming.id, incoming);\n+  return [...items.values()].sort((a, b) => a.seq - b.seq);\n+}\n+\n+// One source of truth for initial load and reconnect.\n+export const initialActivity: ActivityItem[] = [];',
  "console/src/pages/SessionDetailPage.tsx":
    "@@ -142,7 +142,8 @@\n   onActivity: (item) => {\n-    setActivity(previous => [...previous, item]);\n+    setActivity(previous => mergeActivity(previous, item));\n   },\n   onReconnect: () => {\n+    void reloadSession();\n     setConnected(true);\n   },",
  "console/src/api/http.ts":
    "@@ -551,5 +551,8 @@\n   const onMessage = (event: MessageEvent) => {\n+    if (event.lastEventId) {\n+      lastEventId = event.lastEventId;\n+    }\n     const item = normalizeEvent(JSON.parse(event.data));\n-    handlers.onActivity?.(item);\n+    if (item) handlers.onActivity?.(item);\n   };",
  "console/src/__tests__/session-stream.test.ts":
    '@@ -0,0 +1,11 @@\n+describe("session stream reconnect", () => {\n+  it("deduplicates replayed events", () => {\n+    const original = activity({ id: "command-1", seq: 1 });\n+    const replay = { ...original, status: "finished" };\n+    const result = mergeActivity([original], replay);\n+\n+    expect(result).toHaveLength(1);\n+    expect(result[0].status).toBe("finished");\n+  });\n+});',
};
export const demoDiff: SessionChangesDiff = {
  n: 1,
  filesChanged: 4,
  additions: 34,
  deletions: 5,
  files: Object.keys(patches).map((path, i) => ({
    path,
    status: i === 0 || i === 3 ? "added" : "modified",
    additions: [14, 5, 4, 11][i],
    deletions: [0, 3, 2, 0][i],
  })),
};

/** Uses the existing product contract. Every extra state is local demo data. */
export class PrototypeSessionApi extends FixtureSessionApi {
  private previews = new Map<string, Session>();
  private previewHandlers = new Map<string, Set<SessionEventHandlers>>();
  private previewTimers = new Map<string, ReturnType<typeof setTimeout>[]>();
  constructor() {
    super(demoSessions);
  }
  private publish(s: Session) {
    s.updatedAt = new Date().toISOString();
    this.previews.set(s.id, s);
    for (const h of this.previewHandlers.get(s.id) ?? [])
      h.onSession?.(structuredClone(s));
  }
  override async listSessions() {
    return (await super.listSessions())
      .map((s) => structuredClone(this.previews.get(s.id) ?? s))
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }
  override async getSession(id: string) {
    const s = structuredClone(
      this.previews.get(id) ?? (await super.getSession(id)),
    );
    if (s.delivery?.mode === "draft_pr" && s.delivery.status === "delivered")
      s.delivery.prState = "draft";
    return s;
  }
  override async createSession(input: NewSessionInput) {
    // Seeded sessions remain inspectable; new sessions replay the same product
    // lifecycle with local evidence instead of requiring a backend.
    this.autoAdvance = false;
    const s = await super.createSession(input);
    this.autoAdvance = true;
    this.previews.set(s.id, s);
    const stages = [
      { delay: 500, count: 1, phase: "starting" },
      { delay: 1500, count: 4, phase: "running" },
      { delay: 3300, count: 8, phase: "running" },
      { delay: 5200, count: 10, phase: "running" },
      { delay: 7500, count: 12, phase: "running" },
      { delay: 11000, count: 12, phase: "idle" },
    ] as const;
    const timers = stages.map((stage) =>
      setTimeout(() => {
        if (s.phase === "ended") return;
        s.phase = stage.phase;
        s.status = stage.phase === "idle" ? "finished" : "running";
        s.turns[0].status = stage.phase === "idle" ? "finished" : "running";
        s.turns[0].startedAt ??= new Date().toISOString();
        s.turns[0].activity = demoActivity.slice(0, stage.count).map((a) => ({
          ...a,
          ...(stage.phase === "idle" && a.seq === 12
            ? {
                exitCode: 0,
                output: "TypeScript passed. Vite production build completed.",
              }
            : {}),
          ts: new Date().toISOString(),
          text:
            a.kind === "message"
              ? a.seq === 1
                ? "I’ll explore the repository, implement the task, and verify the result. This local demo replays a representative coding workflow."
                : a.seq === 5
                  ? "The relevant code paths are identified. I’m applying the implementation and regression coverage."
                  : "The demo regression checks pass. I’m checking the production build before handing off the changes."
              : a.text,
          status:
            stage.phase === "idle" && a.status === "running"
              ? "finished"
              : a.status,
        }));
        s.hasChanges = stage.count >= 8 && !!s.repo;
        if (stage.phase === "idle") {
          s.turns[0].result =
            "The demo work is complete. Review the representative changes and create a draft pull request when you’re ready.";
          s.turns[0].finishedAt = new Date().toISOString();
        }
        s.lastActivityPreview =
          stage.phase === "idle"
            ? "Demo work complete · ready for review"
            : "Working through the demo task";
        this.publish(s);
      }, stage.delay),
    );
    this.previewTimers.set(s.id, timers);
    return structuredClone(s);
  }
  override async stopSession(id: string) {
    const s = this.previews.get(id);
    if (!s) return super.stopSession(id);
    for (const timer of this.previewTimers.get(id) ?? []) clearTimeout(timer);
    s.phase = "ended";
    s.status = "cancelled";
    s.endReason = "cancelled";
    s.turns.forEach((t) => {
      if (t.status === "running" || t.status === "queued")
        t.status = "cancelled";
    });
    this.publish(s);
    return structuredClone(s);
  }
  override async sendFollowUp(id: string, text: string) {
    const s = this.previews.get(id);
    if (!s) return super.sendFollowUp(id, text);
    if (["ended", "failed"].includes(s.phase))
      throw new Error(
        "This session has ended. Start a new session to continue.",
      );
    const n = ++s.turnCount;
    const turn = {
      ...structuredClone(s.turns[0]),
      id: `turn-${n}`,
      index: n,
      prompt: text,
      status: "queued" as const,
      activity: [],
      result: null,
      createdAt: new Date().toISOString(),
      finishedAt: null,
    };
    s.turns.push(turn);
    this.publish(s);
    const timer = setTimeout(() => {
      if (s.phase === "ended") return;
      const current = s.turns.find((t) => t.id === turn.id)!;
      current.status = "finished";
      current.finishedAt = new Date().toISOString();
      current.result =
        "Follow-up received. This prototype demonstrates continuing work in the same session.";
      if (s.turns[0].status === "finished") {
        s.phase = "idle";
        s.status = "finished";
      }
      this.publish(s);
    }, 1800);
    this.previewTimers.set(id, [...(this.previewTimers.get(id) ?? []), timer]);
    return { session: structuredClone(s), n };
  }
  override async deliverSession(
    id: string,
    input?: DeliverInput,
  ): Promise<SessionDeliverResult> {
    const s = this.previews.get(id);
    if (!s) {
      const result = await super.deliverSession(id, input);
      if (result.session.delivery?.mode === "draft_pr")
        result.session.delivery.prState = "draft";
      return result;
    }
    if (!s.hasChanges)
      throw new Error(
        "Wait for a workspace snapshot before creating a pull request.",
      );
    s.delivery = {
      mode: input?.draft ? "draft_pr" : "pr",
      status: "delivered",
      branch: `sbx/${id}`,
      prNumber: 135,
      prState: input?.draft ? "draft" : "open",
      prBase: s.repo?.ref ?? "main",
    };
    this.publish(s);
    return {
      session: structuredClone(s),
      revision: {
        id: "demo-revision",
        kind: "revision",
        n: 1,
        status: "ready",
        summary: "Demo changes",
        ts: new Date().toISOString(),
        deliveryStatus: "delivered",
      },
    };
  }
  override async listProviders(): Promise<ProviderInfo[]> {
    return PROVIDERS.map((p) => ({
      ...p,
      models: demoModels[p.id],
      accountsTotal: p.id === "codex" ? 2 : 1,
      accountsAvailable: p.id === "opencode" ? 0 : 1,
      readiness: p.id === "opencode" ? "needs_login" : "ready",
    }));
  }
  override async listModels(): Promise<ModelInfo[]> {
    return Object.entries(demoModels).flatMap(([provider, models]) =>
      models.map((model) => ({
        provider,
        model,
        accountsAvailable: 1,
        reasoningEfforts: ["low", "medium", "high", "xhigh"],
        defaultEffort: "high",
      })),
    );
  }
  override async listChangesDiff(id: string): Promise<SessionChangesDiff> {
    if (
      demoSessions.some((s) => s.id === id) ||
      this.previews.get(id)?.hasChanges
    )
      return structuredClone(demoDiff);
    return super.listChangesDiff(id);
  }
  override async getFileDiff(
    id: string,
    path: string,
  ): Promise<SessionFileDiff> {
    if (
      (demoSessions.some((s) => s.id === id) ||
        this.previews.get(id)?.hasChanges) &&
      patches[path]
    ) {
      await new Promise((resolve) => setTimeout(resolve, 180));
      return {
        ...demoDiff.files.find((f) => f.path === path)!,
        diff: patches[path],
      };
    }
    return super.getFileDiff(id, path);
  }
  override subscribe(id: string, handlers: SessionEventHandlers) {
    if (this.previews.has(id)) {
      const set =
        this.previewHandlers.get(id) ?? new Set<SessionEventHandlers>();
      set.add(handlers);
      this.previewHandlers.set(id, set);
      handlers.onSession?.(structuredClone(this.previews.get(id)!));
      return () => {
        set.delete(handlers);
      };
    }
    const unsubscribe = super.subscribe(id, handlers);
    let tick = 0;
    const timer =
      id === "stream-reconnect"
        ? setInterval(() => {
            void super.getSession(id).then((s) => {
              if (s.phase !== "running") return;
              const checks = [
                "TypeScript checking…",
                "Transforming modules…",
                "Rendering production chunks…",
              ];
              handlers.onActivity?.(
                item(12, {
                  kind: "command",
                  status: "running",
                  command: "npm run build",
                  output: checks[tick++ % checks.length],
                }),
              );
            });
          }, 6500)
        : null;
    return () => {
      unsubscribe();
      if (timer) clearInterval(timer);
    };
  }
}

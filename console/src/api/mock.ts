import { ApiError } from "./client";
import type { SessionApi, SessionEventHandlers } from "./client";
import { CHANGES, CREATE_SCRIPT, INTEGRATIONS, MODELS, PROVIDERS, SESSIONS } from "./fixtures";
import { normalizeEvent } from "./normalize";
import type {
  ActivityItem,
  IntegrationStatus,
  ModelInfo,
  NewSessionInput,
  ProviderInfo,
  Session,
  SessionChange,
  SessionPhase,
  Turn,
} from "./types";

export type MockScenario =
  | "ok"
  | "provider_login"
  | "provider_busy"
  | "runtime_disabled"
  | "github_required"
  | "create_fails"
  | "followup_fails"
  | "stream_drops";

interface HandlerEntry {
  sessionId: string;
  handlers: SessionEventHandlers;
}

const SCENARIO_ERRORS: Record<string, ApiError> = {
  provider_login: new ApiError(
    "provider_login",
    "This provider needs a fresh login before it can run sessions",
    { httpStatus: 403, subcode: "auth_invalid", retryable: false },
  ),
  provider_busy: new ApiError(
    "provider_busy",
    "Every account for this provider is busy right now",
    { httpStatus: 429, subcode: "provider_exhausted", retryable: true, retryAfter: 30 },
  ),
  runtime_disabled: new ApiError(
    "runtime_disabled",
    "The sandbox runtime is disabled for this deployment",
    { httpStatus: 503, subcode: "runtime_disabled", retryable: true },
  ),
  github_required: new ApiError(
    "github_required",
    "Connect GitHub to run sessions against a repository",
    { httpStatus: 503, subcode: "github_app_unconfigured", retryable: false },
  ),
  create_fails: new ApiError(
    "session_failed",
    "The session could not be started",
    { httpStatus: 500, subcode: "session_not_runnable", retryable: true },
  ),
  followup_fails: new ApiError(
    "conflict",
    "A turn is already in progress — wait for it to finish",
    { httpStatus: 409, subcode: "turn_in_progress", retryable: true },
  ),
};

/** V2-shaped frames (annotated with ``n`` at emit time). */
const FOLLOWUP_SCRIPT: Record<string, unknown>[] = [
  { type: "turn.started" },
  {
    type: "item.completed",
    item: {
      id: "fu-1",
      type: "reasoning",
      text: "Re-reading the touched files to apply the follow-up…",
    },
  },
  {
    type: "item.completed",
    item: {
      id: "fu-2",
      type: "command_execution",
      command: "uv run pytest tests/unit -q",
      aggregated_output: "12 passed in 1.10s",
      exit_code: 0,
    },
  },
  {
    type: "item.completed",
    item: {
      id: "fu-3",
      type: "agent_message",
      text: "Applied the follow-up and re-ran the unit tests — all green.",
    },
  },
  { type: "turn.finished", status: "finished" },
];

const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T;

export class FixtureSessionApi implements SessionApi {
  /** Switch behaviors for dev/testing. */
  scenario: MockScenario = "ok";
  /** Artificial latency per call (ms). */
  latencyMs = 120;
  /** Set false to freeze scripted phase/event progression. */
  autoAdvance = true;

  private sessions = new Map<string, Session>();
  private changes = new Map<string, SessionChange[]>();
  private handlers = new Set<HandlerEntry>();
  private timers = new Set<ReturnType<typeof setTimeout>>();
  private seq = 1000;

  constructor(seed: Session[] = SESSIONS) {
    for (const s of seed) this.sessions.set(s.id, clone(s));
    for (const [k, v] of Object.entries(CHANGES)) this.changes.set(k, clone(v));
  }

  private wait<T>(v: T, ms = this.latencyMs): Promise<T> {
    return new Promise((resolve) => setTimeout(() => resolve(clone(v)), ms));
  }

  private later(fn: () => void, ms: number) {
    const t = setTimeout(() => {
      this.timers.delete(t);
      fn();
    }, ms);
    this.timers.add(t);
    return t;
  }

  private emit(sessionId: string, fn: (h: SessionEventHandlers) => void) {
    for (const entry of this.handlers) {
      if (entry.sessionId === sessionId) fn(entry.handlers);
    }
  }

  private update(session: Session) {
    session.updatedAt = new Date().toISOString();
    this.sessions.set(session.id, session);
    this.emit(session.id, (h) => h.onSession?.(clone(session)));
  }

  private setPhase(session: Session, phase: SessionPhase, status?: Session["status"]) {
    session.phase = phase;
    if (status) session.status = status;
    else {
      session.status =
        phase === "queued"
          ? "queued"
          : phase === "running" || phase === "starting"
            ? "running"
            : phase === "idle"
              ? "finished"
              : phase === "ended"
                ? "cancelled"
                : phase === "failed"
                  ? "failed"
                  : session.status;
    }
    this.update(session);
    this.emit(session.id, (h) => h.onPhase?.(phase));
  }

  private pushActivity(session: Session, item: ActivityItem) {
    const turn = session.turns.find((t) => t.id === item.turnId);
    if (turn) {
      const idx = turn.activity.findIndex((a) => a.id === item.id);
      if (idx >= 0) turn.activity[idx] = item;
      else turn.activity.push(item);
    }
    session.lastActivityPreview =
      item.text ?? item.output ?? item.command ?? item.status ?? item.kind;
    this.update(session);
    this.emit(session.id, (h) => h.onActivity?.(clone(item)));
    if (turn) this.emit(session.id, (h) => h.onTurn?.(clone(turn)));
  }

  private scenarioError(key: keyof typeof SCENARIO_ERRORS): ApiError | null {
    return this.scenario === key ? SCENARIO_ERRORS[key] : null;
  }

  async listSessions(): Promise<Session[]> {
    const err = this.scenarioError("runtime_disabled");
    if (err) throw err;
    const all = [...this.sessions.values()].sort((a, b) =>
      b.updatedAt.localeCompare(a.updatedAt),
    );
    return this.wait(all);
  }

  async getSession(id: string): Promise<Session> {
    const s = this.sessions.get(id);
    if (!s) {
      throw new ApiError("not_found", "Session not found", {
        httpStatus: 404,
        subcode: "not_found",
      });
    }
    return this.wait(s);
  }

  async createSession(input: NewSessionInput): Promise<Session> {
    for (const key of [
      "provider_login",
      "provider_busy",
      "runtime_disabled",
      "github_required",
      "create_fails",
    ] as const) {
      const err = this.scenarioError(key);
      if (err) {
        await this.wait(null);
        throw err;
      }
    }
    if (input.repo && !INTEGRATIONS.github.connected) {
      await this.wait(null);
      throw SCENARIO_ERRORS.github_required;
    }

    const id = `sess-${Math.random().toString(16).slice(2, 18)}`;
    const provider =
      input.provider && input.provider !== "auto" ? input.provider : "codex";
    const providerInfo = PROVIDERS.find((p) => p.id === provider) ?? PROVIDERS[0];
    const model =
      input.model && input.model !== "auto" ? input.model : providerInfo.models[0];
    const title =
      input.title?.trim() || input.prompt.split("\n")[0].slice(0, 80) || "New session";
    const firstTurn: Turn = {
      id: "turn-1",
      index: 1,
      prompt: input.prompt,
      status: "queued",
      createdAt: new Date().toISOString(),
      startedAt: null,
      finishedAt: null,
      result: null,
      error: null,
      activity: [],
    };
    const session: Session = {
      id,
      title,
      status: "queued",
      phase: "queued",
      endReason: null,
      prompt: input.prompt,
      provider,
      model,
      accountLabel: null,
      repo: input.repo
        ? {
            name: input.repo,
            url: `https://github.com/${input.repo.replace(/^https:\/\/github\.com\//, "")}`,
            ref: input.repoRef,
          }
        : null,
      effort: input.effort ?? null,
      compute: input.compute
        ? {
            cpu: [input.compute.cpu ?? 1, input.compute.cpu ?? 2],
            memoryMib: [input.compute.memoryMib ?? 1024, input.compute.memoryMib ?? 8192],
          }
        : null,
      idleTimeoutS: input.idleTimeoutS ?? null,
      delivery: input.delivery && input.delivery !== "none"
        ? {
            mode: input.delivery,
            status: "pending",
            prState: input.delivery === "draft_pr" ? "draft" : undefined,
          }
        : null,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      usage: null,
      costUsd: null,
      turnCount: 1,
      turns: [firstTurn],
      lastActivityPreview: null,
      hasChanges: false,
      error: null,
    };
    this.sessions.set(id, session);

    if (this.autoAdvance) {
      let offset = 0;
      for (const step of CREATE_SCRIPT) {
        offset = step.delayMs;
        this.later(() => this.setPhase(session, step.phase), step.delayMs);
      }
      const first = session.turns[0];
      this.later(() => {
        first.status = "running";
        first.startedAt = new Date().toISOString();
        this.update(session);
        this.emit(session.id, (h) => h.onTurn?.(clone(first)));
        this.emit(session.id, (h) =>
          h.onMeta?.({ provider: session.provider, model: session.model }),
        );
      }, offset + 200);
      this.later(() => {
        first.status = "finished";
        first.finishedAt = new Date().toISOString();
        this.setPhase(session, "idle", "finished");
      }, offset + 2600);
    }
    return this.wait(session);
  }

  async sendFollowUp(
    sessionId: string,
    text: string,
  ): Promise<{ session: Session; n: number | null }> {
    const session = this.sessions.get(sessionId);
    if (!session) {
      throw new ApiError("not_found", "Session not found", {
        httpStatus: 404,
        subcode: "not_found",
      });
    }
    if (session.phase === "ended" || session.phase === "failed") {
      throw new ApiError(
        "session_failed",
        "This session has ended — start a new one to keep working",
        { httpStatus: 409, subcode: "session_not_runnable" },
      );
    }
    const err = this.scenarioError("followup_fails");
    if (err) {
      await this.wait(null);
      throw err;
    }
    const n = session.turnCount + 1;
    const turn: Turn = {
      id: `turn-${n}`,
      index: n,
      prompt: text,
      status: "running",
      createdAt: new Date().toISOString(),
      startedAt: new Date().toISOString(),
      finishedAt: null,
      result: null,
      error: null,
      activity: [],
    };
    session.turnCount = n;
    session.turns.push(turn);
    session.status = "running";
    session.phase = "running";
    this.update(session);
    this.later(() => {
      this.emit(sessionId, (h) => h.onTurn?.(clone(turn)));
      this.emit(sessionId, (h) => h.onPhase?.("running"));
    }, 0);

    if (this.autoAdvance) {
      let i = 0;
      for (const raw of FOLLOWUP_SCRIPT) {
        const frame = { ...raw, n };
        this.later(() => {
          const item = normalizeEvent(frame, sessionId);
          if (item) {
            item.seq = ++this.seq;
            this.pushActivity(session, item);
          }
          if (raw.type === "turn.finished") {
            turn.status = "finished";
            turn.finishedAt = new Date().toISOString();
            turn.result =
              "Applied the follow-up and re-ran the unit tests — all green.";
            this.setPhase(session, "idle", "finished");
          }
        }, 400 + i * 450);
        i += 1;
      }
    }
    return this.wait({ session, n }, 60);
  }

  async stopSession(sessionId: string): Promise<Session> {
    const session = this.sessions.get(sessionId);
    if (!session) {
      throw new ApiError("not_found", "Session not found", {
        httpStatus: 404,
        subcode: "not_found",
      });
    }
    const active = session.turns.find(
      (t) => t.status === "running" || t.status === "queued",
    );
    if (active) {
      active.status = "cancelled";
      active.finishedAt = new Date().toISOString();
    }
    this.setPhase(session, "ended", "cancelled");
    session.endReason = "cancelled";
    this.update(session);
    return this.wait(session, 40);
  }

  async retrySession(sessionId: string, prompt?: string): Promise<Session> {
    const session = this.sessions.get(sessionId);
    if (!session) {
      throw new ApiError("not_found", "Session not found", {
        httpStatus: 404,
        subcode: "not_found",
      });
    }
    if (session.phase !== "failed") {
      throw new ApiError("conflict", "Nothing to retry", {
        httpStatus: 409,
        subcode: "task_not_retryable",
      });
    }
    const n = session.turnCount + 1;
    const turn: Turn = {
      id: `turn-${n}`,
      index: n,
      prompt: prompt?.trim() || session.prompt,
      status: "queued",
      createdAt: new Date().toISOString(),
      startedAt: null,
      finishedAt: null,
      result: null,
      error: null,
      activity: [],
    };
    session.turnCount = n;
    session.turns.push(turn);
    session.error = null;
    this.setPhase(session, "queued", "queued");
    this.later(() => {
      this.emit(sessionId, (h) => h.onTurn?.(clone(turn)));
    }, 0);
    if (this.autoAdvance) {
      this.later(() => this.setPhase(session, "running", "running"), 500);
      this.later(() => {
        turn.status = "finished";
        turn.finishedAt = new Date().toISOString();
        turn.result = "Retry completed successfully.";
        this.pushActivity(session, {
          id: `fx-retry-${n}`,
          seq: ++this.seq,
          ts: new Date().toISOString(),
          turnId: turn.id,
          n,
          kind: "message",
          role: "assistant",
          text: turn.result,
          status: "finished",
        });
        this.setPhase(session, "idle", "finished");
      }, 1400);
    }
    return this.wait(session, 60);
  }

  async deliverSession(sessionId: string): Promise<Session> {
    const session = this.sessions.get(sessionId);
    if (!session) {
      throw new ApiError("not_found", "Session not found", {
        httpStatus: 404,
        subcode: "not_found",
      });
    }
    if (!session.hasChanges && !session.delivery) {
      throw new ApiError("conflict", "No changes to deliver", {
        httpStatus: 409,
        subcode: "revision_not_ready",
      });
    }
    const n = (this.changes.get(sessionId) ?? [])
        .filter((c) => c.kind === "revision")
        .length + 1;
    const change: SessionChange = {
      id: `rev-${n}`,
      kind: "revision",
      n,
      status: "delivered",
      summary: `revision ${n}`,
      ts: new Date().toISOString(),
      branch: session.delivery?.branch ?? `sbx/${sessionId}-1`,
    };
    this.changes.set(sessionId, [...(this.changes.get(sessionId) ?? []), change]);
    session.hasChanges = true;
    if (session.delivery) session.delivery.status = "delivered";
    else session.delivery = { mode: "branch", status: "delivered", branch: change.branch };
    this.update(session);
    return this.wait(session, 60);
  }

  async listProviders(): Promise<ProviderInfo[]> {
    return this.wait(clone(PROVIDERS));
  }

  async listModels(): Promise<ModelInfo[]> {
    return this.wait(clone(MODELS));
  }

  async getIntegrations(): Promise<IntegrationStatus> {
    return this.wait(clone(INTEGRATIONS));
  }

  async beginGithubAuthorize(): Promise<{ url: string }> {
    return this.wait({ url: "https://github.com/apps/sbx-browser/installations/new" }, 40);
  }

  async listChanges(sessionId: string): Promise<SessionChange[]> {
    return this.wait(clone(this.changes.get(sessionId) ?? []));
  }

  subscribe(sessionId: string, handlers: SessionEventHandlers): () => void {
    const entry: HandlerEntry = { sessionId, handlers };
    this.handlers.add(entry);
    // Replay the current snapshot so late subscribers catch up.
    const session = this.sessions.get(sessionId);
    if (session) {
      handlers.onSession?.(clone(session));
      handlers.onPhase?.(session.phase);
    }
    if (this.scenario === "stream_drops") {
      this.later(() => handlers.onDisconnect?.(1500), 300);
      this.later(() => handlers.onReconnect?.(), 2000);
    }
    return () => {
      this.handlers.delete(entry);
    };
  }

  dispose() {
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
    this.handlers.clear();
  }
}

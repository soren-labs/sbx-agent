import { ApiError } from "./client";
import type { SessionApi, SessionEventHandlers } from "./client";
import { CHANGES, CREATE_SCRIPT, INTEGRATIONS, PROVIDERS, SESSIONS } from "./fixtures";
import { normalizeEvent } from "./normalize";
import type {
  ActivityItem,
  IntegrationStatus,
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
    { httpStatus: 400, subcode: "github_required", retryable: false },
  ),
  create_fails: new ApiError(
    "session_failed",
    "The session could not be started",
    { httpStatus: 500, subcode: "session_failed", retryable: true },
  ),
  followup_fails: new ApiError(
    "session_failed",
    "The follow-up could not be delivered",
    { httpStatus: 409, subcode: "turn_in_progress", retryable: true },
  ),
};

const FOLLOWUP_SCRIPT: Record<string, unknown>[] = [
  { type: "turn.started" },
  {
    type: "item.completed",
    item: {
      type: "reasoning",
      text: "Re-reading the touched files to apply the follow-up…",
    },
  },
  {
    type: "item.completed",
    item: {
      type: "command_execution",
      command: "uv run pytest tests/unit -q",
      exit_code: 0,
      output: "12 passed in 1.10s",
    },
  },
  {
    type: "item.completed",
    item: {
      type: "agent_message",
      text: "Applied the follow-up and re-ran the unit tests — all green.",
    },
  },
  { type: "turn.completed" },
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

  private setPhase(session: Session, phase: SessionPhase) {
    session.phase = phase;
    this.update(session);
    this.emit(session.id, (h) => h.onPhase?.(phase));
  }

  private pushActivity(session: Session, item: ActivityItem) {
    const turn = session.turns.find((t) => t.id === item.turnId);
    if (turn) turn.activity.push(item);
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

    const id = `sess-${Math.random().toString(16).slice(2, 8)}`;
    const provider =
      input.provider && input.provider !== "auto" ? input.provider : "codex";
    const providerInfo =
      PROVIDERS.find((p) => p.id === provider) ?? PROVIDERS[0];
    const model =
      input.model && input.model !== "auto"
        ? input.model
        : providerInfo.models[0];
    const title =
      input.title?.trim() ||
      input.prompt.split("\n")[0].slice(0, 80) ||
      "New session";
    const firstTurn: Turn = {
      id: `turn-${id}-1`,
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
      phase: "queued",
      endReason: null,
      provider,
      model,
      accountLabel: `${provider}/auto`,
      repo: input.repo
        ? {
            name: input.repo,
            url: `https://github.com/${input.repo.replace(/^https:\/\/github\.com\//, "")}`,
          }
        : null,
      effort: input.effort ?? null,
      compute: input.compute
        ? {
            cpu: [input.compute.cpu ?? 1, input.compute.cpu ?? 2],
            memoryMib: [
              input.compute.memoryMib ?? 1024,
              input.compute.memoryMib ?? 8192,
            ],
          }
        : { cpu: [1, 2], memoryMib: [1024, 8192] },
      idleTimeoutS: input.idleTimeoutS ?? 1800,
      delivery: { mode: input.delivery ?? "none", target: input.deliveryTarget },
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      usage: null,
      costUsd: null,
      runtimeSeconds: null,
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
      }, offset + 200);
      this.later(() => this.setPhase(session, "idle"), offset + 2600);
    }
    return this.wait(session);
  }

  async sendFollowUp(
    sessionId: string,
    text: string,
  ): Promise<{ turnId: string }> {
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
    const turn: Turn = {
      id: `turn-${sessionId}-${session.turns.length + 1}`,
      index: session.turns.length + 1,
      prompt: text,
      status: "running",
      createdAt: new Date().toISOString(),
      startedAt: new Date().toISOString(),
      finishedAt: null,
      result: null,
      error: null,
      activity: [],
    };
    session.turns.push(turn);
    this.update(session);
    this.later(() => {
      this.emit(sessionId, (h) => h.onTurn?.(clone(turn)));
      this.setPhase(session, "running");
    }, 0);

    if (this.autoAdvance) {
      let i = 0;
      for (const raw of FOLLOWUP_SCRIPT) {
        const frame = { ...raw, turn_id: turn.id };
        this.later(() => {
          const item = normalizeEvent(frame);
          if (item) {
            item.seq = ++this.seq;
            this.pushActivity(session, item);
          }
          if (raw.type === "turn.completed") {
            turn.status = "finished";
            turn.finishedAt = new Date().toISOString();
            this.setPhase(session, "idle");
          }
        }, 400 + i * 450);
        i += 1;
      }
    }
    return this.wait({ turnId: turn.id }, 60);
  }

  async stopSession(sessionId: string): Promise<void> {
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
    if (session.phase === "running") session.phase = "idle";
    this.update(session);
    await this.wait(null, 40);
  }

  async closeSession(sessionId: string): Promise<Session> {
    const session = this.sessions.get(sessionId);
    if (!session) {
      throw new ApiError("not_found", "Session not found", {
        httpStatus: 404,
        subcode: "not_found",
      });
    }
    session.phase = "ended";
    session.endReason = "closed";
    this.update(session);
    this.emit(sessionId, (h) => h.onPhase?.("ended"));
    return this.wait(session, 40);
  }

  async listProviders(): Promise<ProviderInfo[]> {
    return this.wait(clone(PROVIDERS));
  }

  async getIntegrations(): Promise<IntegrationStatus> {
    return this.wait(clone(INTEGRATIONS));
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

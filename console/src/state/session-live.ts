import type { ApiClient } from "../api/client";
import { isApiError, isResyncError } from "../api/errors";
import type { ExecutorView, Session, Turn } from "../api/types";
import {
  CLEAN,
  initialLiveState,
  liveReducer,
  type Dirty,
  type LiveAction,
  type LiveState,
} from "./session-events";

export type LiveStatus = "loading" | "live" | "reconnecting" | "error";

export interface SessionLiveState extends LiveState {
  status: LiveStatus;
  session: Session | null;
  turns: Turn[];
  executor: ExecutorView | null;
  error: unknown;
  /** Bumped when a non-conversation projection (changes, deliveries…) is dirty. */
  revisions: Record<"changes" | "deliveries" | "delegations" | "services", number>;
}

type LiveApi = Pick<ApiClient, "sessions">;

export interface LiveOptions {
  reconnectMs?: number;
  refreshDebounceMs?: number;
}

/**
 * Per-session snapshot + committed-event store. Snapshot and watermark come from
 * one response; replay starts after it; reconnects only replay (they never POST).
 */
export class SessionLive {
  private state: SessionLiveState;
  private listeners = new Set<() => void>();
  private stopped = true;
  private gen = 0;
  private abort: AbortController | null = null;
  private refreshTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly reconnectMs: number;
  private readonly debounceMs: number;

  constructor(
    private readonly api: LiveApi,
    readonly sessionId: string,
    opts: LiveOptions = {},
  ) {
    this.reconnectMs = opts.reconnectMs ?? 1500;
    this.debounceMs = opts.refreshDebounceMs ?? 120;
    this.state = {
      ...initialLiveState(),
      status: "loading",
      session: null,
      turns: [],
      executor: null,
      error: null,
      revisions: { changes: 0, deliveries: 0, delegations: 0, services: 0 },
    };
  }

  getState = () => this.state;
  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };
  private set(patch: Partial<SessionLiveState>) {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((l) => l());
  }
  private dispatch(action: LiveAction) {
    const next = liveReducer(this.state, action);
    if (next !== this.state) this.set(next);
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    void this.run(++this.gen);
  }

  stop() {
    this.stopped = true;
    this.gen++;
    this.abort?.abort();
    if (this.refreshTimer) clearTimeout(this.refreshTimer);
    this.refreshTimer = null;
  }

  /** Refetch server projections (used after local commands and on dirty events). */
  refresh(keys: (keyof Dirty)[] = ["session", "messages", "turns", "executor"]) {
    const want = new Set(keys);
    void Promise.allSettled([
      want.has("session") &&
        this.api.sessions.get(this.sessionId).then((r) => this.set({ session: r.session })),
      want.has("messages") &&
        this.api.sessions
          .messages(this.sessionId)
          .then((r) => this.dispatch({ type: "merge_messages", messages: r.items })),
      want.has("turns") &&
        this.api.sessions.turns(this.sessionId).then((r) => this.set({ turns: r.items })),
      want.has("executor") &&
        this.api.sessions.executor(this.sessionId).then((r) => this.set({ executor: r })),
    ]);
  }

  private async snapshot() {
    const [s, m, t, x] = await Promise.all([
      this.api.sessions.get(this.sessionId),
      this.api.sessions.messages(this.sessionId),
      this.api.sessions.turns(this.sessionId).catch(() => null),
      this.api.sessions.executor(this.sessionId).catch(() => null),
    ]);
    this.dispatch({ type: "snapshot", messages: m.items, watermark: m.event_watermark });
    this.set({
      session: s.session,
      turns: t?.items ?? this.state.turns,
      executor: x ?? this.state.executor,
      error: null,
    });
  }

  private scheduleRefresh() {
    if (this.refreshTimer) return;
    this.refreshTimer = setTimeout(() => {
      this.refreshTimer = null;
      const d = this.state.dirty;
      const keys = (["session", "messages", "turns", "executor"] as const).filter((k) => d[k]);
      const rev = { ...this.state.revisions };
      for (const k of ["changes", "deliveries", "delegations", "services"] as const) {
        if (d[k]) rev[k] += 1;
      }
      this.dispatch({ type: "clear_dirty", keys: Object.keys(CLEAN) as (keyof Dirty)[] });
      this.set({ revisions: rev });
      if (keys.length) this.refresh([...keys]);
    }, this.debounceMs);
  }

  private sleep(ms: number) {
    return new Promise<void>((r) => setTimeout(r, ms));
  }

  private async run(gen: number) {
    const alive = () => !this.stopped && gen === this.gen;
    let needSnapshot = true;
    while (alive()) {
      try {
        if (needSnapshot) {
          await this.snapshot();
          needSnapshot = false;
        }
        this.set({ status: "live" });
        const ctl = (this.abort = new AbortController());
        await this.api.sessions.streamEvents(this.sessionId, {
          after: this.state.lastSeq,
          signal: ctl.signal,
          onEvent: (e) => {
            this.dispatch({ type: "events", items: [e] });
            if (this.state.needsResync) ctl.abort();
            else this.scheduleRefresh();
          },
        });
        if (this.state.needsResync) {
          needSnapshot = true;
          continue;
        }
      } catch (err) {
        if (!alive()) return;
        if (isResyncError(err)) {
          needSnapshot = true;
          continue;
        }
        if (isApiError(err) && !err.retryable && err.status >= 400 && err.status < 500) {
          this.set({ status: "error", error: err });
          return;
        }
        if (this.state.session === null) needSnapshot = true;
        this.set({ status: this.state.session ? "reconnecting" : "loading", error: err });
      }
      if (!alive()) return;
      if (this.state.status === "live") this.set({ status: "reconnecting" });
      await this.sleep(this.reconnectMs);
    }
  }
}

/**
 * One typed query/event store for the unified console (RFC 167 §08).
 *
 * - Reducers dedupe events by `seq`/id; caches are disposable snapshots.
 * - No UI computes terminal success from heuristics — `state`/`verdict`
 *   fields come straight from the API.
 * - Watermarks come from the same snapshot that returned the events, so a
 *   re-poll always asks for `> watermark` of what was committed when the
 *   last batch was read.
 * - The login token lives in memory only (the `sbx_session` cookie
 *   re-authenticates after reload); no secrets hit localStorage.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api, type SessionEvent, type User } from "../api/unified";

interface UnifiedState {
  user: User | null;
  workspaceId: string | null;
  /** sessionId → deduped event replay */
  events: Record<string, { items: SessionEvent[]; watermark: number; seen: number[] }>;
}

type Action =
  | { kind: "auth"; user: User | null; workspaceId: string | null }
  | { kind: "events"; sessionId: string; items: SessionEvent[]; watermark: number }
  | { kind: "events_reset"; sessionId: string };

const initial: UnifiedState = { user: null, workspaceId: null, events: {} };

interface EventTail {
  items: SessionEvent[];
  watermark: number;
  seen: number[];
}

/** Committed-replay merge: dedupe by seq, never lower the watermark. */
export function mergeEvents(
  cur: EventTail | undefined,
  items: SessionEvent[],
  watermark: number,
): EventTail {
  const base = cur ?? { items: [], watermark: 0, seen: [] };
  const seen = new Set(base.seen);
  const fresh = items.filter((e) => !seen.has(e.seq));
  for (const e of fresh) seen.add(e.seq);
  return {
    items: [...base.items, ...fresh].sort((a, b) => a.seq - b.seq),
    watermark: Math.max(base.watermark, watermark),
    seen: [...seen],
  };
}

function reducer(state: UnifiedState, action: Action): UnifiedState {
  switch (action.kind) {
    case "auth":
      return { ...state, user: action.user, workspaceId: action.workspaceId };
    case "events": {
      const cur = state.events[action.sessionId];
      const next = mergeEvents(cur, action.items, action.watermark);
      if (
        cur &&
        next.items.length === cur.items.length &&
        next.watermark === cur.watermark
      ) {
        return state;
      }
      return {
        ...state,
        events: { ...state.events, [action.sessionId]: next },
      };
    }
    case "events_reset": {
      const events = { ...state.events };
      delete events[action.sessionId];
      return { ...state, events };
    }
  }
}

interface UnifiedCtx {
  state: UnifiedState;
  /** 'loading' until the first /api/me attempt resolves */
  ready: boolean;
  signIn(email: string, password: string): Promise<void>;
  signUp(email: string, password: string): Promise<void>;
  signOut(): Promise<void>;
  setWorkspace(id: string): void;
  eventsFor(sessionId: string): { items: SessionEvent[]; watermark: number };
  ingestEvents(sessionId: string, items: SessionEvent[], watermark: number): void;
}

const Ctx = createContext<UnifiedCtx | null>(null);

export function UnifiedProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initial);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    // Cookie re-auth on reload — the cookie, not a stored token, carries it.
    api
      .me()
      .then((u) => {
        dispatch({
          kind: "auth",
          user: u,
          workspaceId: u.workspaces[0] ?? null,
        });
      })
      .catch(() => dispatch({ kind: "auth", user: null, workspaceId: null }))
      .finally(() => setReady(true));
  }, []);

  const signIn = useCallback(async (email: string, password: string) => {
    const r = await api.login(email, password);
    api.setToken(r.token);
    dispatch({
      kind: "auth",
      user: r.user,
      workspaceId: r.user.workspaces[0] ?? null,
    });
  }, []);

  const signUp = useCallback(async (email: string, password: string) => {
    await api.register(email, password);
    await signIn(email, password);
  }, [signIn]);

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      api.setToken(null);
      dispatch({ kind: "auth", user: null, workspaceId: null });
    }
  }, []);

  const setWorkspace = useCallback((id: string) => {
    dispatch({ kind: "auth", user: state.user, workspaceId: id });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const eventsFor = useCallback(
    (sessionId: string) =>
      state.events[sessionId] ?? { items: [], watermark: 0 },
    [state.events],
  );

  const ingestEvents = useCallback(
    (sessionId: string, items: SessionEvent[], watermark: number) =>
      dispatch({ kind: "events", sessionId, items, watermark }),
    [],
  );

  const value = useMemo(
    () => ({
      state,
      ready,
      signIn,
      signUp,
      signOut,
      setWorkspace,
      eventsFor,
      ingestEvents,
    }),
    [state, ready, signIn, signUp, signOut, setWorkspace, eventsFor, ingestEvents],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useUnified(): UnifiedCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useUnified outside UnifiedProvider");
  return ctx;
}

/** Poll a resource on an interval while `active` — purely additive cache. */
export function usePoll<T>(
  fn: () => Promise<T>,
  ms: number,
  deps: unknown[] = [],
): { data: T | null; error: Error | null; refresh: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const load = useCallback(() => {
    fnRef
      .current()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(e as Error));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => {
    load();
    const t = window.setInterval(load, ms);
    return () => window.clearInterval(t);
  }, [load, ms]);
  return { data, error, refresh: load };
}

/** Tail the committed event stream for a session into the store. */
export function useEventTail(sessionId: string | null, ms = 1500) {
  const { state, ingestEvents } = useUnified();
  const lastSeqRef = useRef(0);
  useEffect(() => {
    if (!sessionId) return;
    lastSeqRef.current = 0;
    let stopped = false;
    const tick = async () => {
      if (stopped) return;
      try {
        const r = await api.listEvents(sessionId, lastSeqRef.current, 500);
        if (r.items.length) {
          lastSeqRef.current = r.items[r.items.length - 1].seq;
        }
        ingestEvents(sessionId, r.items, r.event_watermark);
      } catch {
        /* transient — retried next tick */
      }
    };
    void tick();
    const t = window.setInterval(() => void tick(), ms);
    return () => {
      stopped = true;
      window.clearInterval(t);
    };
  }, [sessionId, ms, ingestEvents]);
  return (
    (sessionId ? state.events[sessionId] : undefined) ?? {
      items: [],
      watermark: 0,
      seen: [],
    }
  );
}

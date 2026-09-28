import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { ApiError, isApiError } from "../api";
import type {
  ActivityItem,
  Session,
  SessionChange,
  Turn,
} from "../api/types";
import { Conversation, ActivityTimeline } from "../components/Conversation";
import { ErrorNotice, ReconnectBanner } from "../components/ErrorNotice";
import { FollowUp } from "../components/FollowUp";
import { Spinner } from "../components/icons";
import { ChangesPanel, SessionMeta } from "../components/SessionMeta";
import { ProviderBadge, StatusPill } from "../components/StatusPill";
import { useI18n } from "../i18n";
import { useApi } from "../state/api";

type Tab = "conversation" | "activity" | "changes";

/** Merge a turn into the list, keyed on the wire ``n`` (turn-<n> id). Live
 * frames carry no prompt — keep the existing one. */
function mergeTurn(turns: Turn[], next: Turn): Turn[] {
  const idx = turns.findIndex((x) => x.id === next.id);
  if (idx < 0) return [...turns, next];
  return turns.map((x, i) =>
    i === idx
      ? {
          ...x,
          ...next,
          prompt: next.prompt || x.prompt,
          activity:
            next.activity.length > 0
              ? dedupeItems([...x.activity, ...next.activity])
              : x.activity,
          startedAt: next.startedAt ?? x.startedAt,
          result: next.result ?? x.result,
        }
      : x,
  );
}

/** item.started → item.completed share ``item-<id>``; replace in place. */
function dedupeItems(items: ActivityItem[]): ActivityItem[] {
  const seen = new Map<string, ActivityItem>();
  for (const it of items) seen.set(it.id, it);
  return [...seen.values()].sort((a, b) => a.seq - b.seq);
}

/** Merge a session-level update that carries no turn detail. */
function mergeSessionMeta(prev: Session, next: Session): Session {
  return {
    ...prev,
    ...next,
    turns: next.turns.length > 0 ? next.turns.reduce(mergeTurn, prev.turns) : prev.turns,
    turnCount: Math.max(prev.turnCount, next.turnCount),
  };
}

export function SessionDetailPage() {
  const { t } = useI18n();
  const api = useApi();
  const { id = "" } = useParams();
  const location = useLocation();
  const optimistic = (location.state as { session?: Session } | null)?.session;

  const [session, setSession] = useState<Session | null>(optimistic ?? null);
  const [changes, setChanges] = useState<SessionChange[]>([]);
  // Session-level events (status transitions, errors with no turn).
  const [feed, setFeed] = useState<ActivityItem[]>([]);
  const [loading, setLoading] = useState(!optimistic);
  const [error, setError] = useState<ApiError | null>(null);
  const [tab, setTab] = useState<Tab>("conversation");
  const [retryInMs, setRetryInMs] = useState<number | null>(null);
  const [reconnected, setReconnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const seqRef = useRef(10_000);
  const bottomRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      const s = await api.getSession(id);
      setSession(s);
      setError(null);
      if (s.hasChanges || s.delivery) {
        setChanges(await api.listChanges(id).catch(() => []));
      }
    } catch (e) {
      setError(isApiError(e) ? e : null);
    } finally {
      setLoading(false);
    }
  }, [api, id]);

  useEffect(() => {
    void load();
  }, [load]);

  // Live subscription: session-scoped stream covering every turn — phases,
  // activity items (deduped), turn updates, meta, stream drops.
  useEffect(() => {
    if (!id) return;
    const unsub = api.subscribe(id, {
      onPhase: (phase) =>
        setSession((s) => (s ? { ...s, phase } : s)),
      onSession: (next) =>
        setSession((s) => (s ? mergeSessionMeta(s, next) : next)),
      onMeta: (meta) =>
        setSession((s) =>
          s
            ? {
                ...s,
                provider: meta.provider ?? s.provider,
                model: meta.model ?? s.model,
              }
            : s,
        ),
      onTurn: (turn) =>
        setSession((s) =>
          s ? { ...s, turns: mergeTurn(s.turns, turn) } : s,
        ),
      onActivity: (item) => {
        const turnId = item.turnId;
        if (!turnId) {
          // Session-level frame (status transitions) — Activity tab only.
          setFeed((f) => dedupeItems([...f, item]));
          return;
        }
        setSession((s) => {
          if (!s) return s;
          const idx = s.turns.findIndex((tn) => tn.id === turnId);
          if (idx >= 0) {
            const turns = s.turns.map((tn, i) =>
              i === idx
                ? { ...tn, activity: dedupeItems([...tn.activity, item]) }
                : tn,
            );
            return { ...s, turns };
          }
          // Item for a turn we haven't seen — create a placeholder so live
          // items are never dropped (turn.started may arrive after items).
          const n = item.n ?? s.turns.length + 1;
          const placeholder: Turn = {
            id: turnId,
            index: n,
            prompt: "",
            status: "running",
            createdAt: item.ts,
            startedAt: item.ts,
            finishedAt: null,
            result: null,
            error: null,
            activity: [item],
          };
          return { ...s, turns: [...s.turns, placeholder] };
        });
      },
      onDisconnect: (ms) => {
        setReconnected(false);
        setRetryInMs(ms);
      },
      onReconnect: () => {
        setRetryInMs(null);
        setReconnected(true);
        void load();
        setTimeout(() => setReconnected(false), 3000);
      },
      onError: (e) => setError(e),
    });
    return unsub;
  }, [api, id, load]);

  // Follow the conversation tail on new activity.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [session?.turns.length, session?.updatedAt]);

  const sendFollowUp = async (text: string) => {
    // Optimistic turn so the prompt lands instantly.
    const pendingId = `pending-${++seqRef.current}`;
    const pending: Turn = {
      id: pendingId,
      index: (session?.turnCount ?? session?.turns.length ?? 0) + 1,
      prompt: text,
      status: "queued",
      createdAt: new Date().toISOString(),
      startedAt: null,
      finishedAt: null,
      result: null,
      error: null,
      activity: [],
    };
    setSession((s) => (s ? { ...s, turns: [...s.turns, pending] } : s));
    try {
      const { session: next, n } = await api.sendFollowUp(id, text);
      setSession((s) => {
        if (!s) return s;
        const merged = mergeSessionMeta(s, next);
        if (n != null) {
          // Re-key the optimistic pending row onto the real turn n.
          merged.turns = merged.turns.map((tn) =>
            tn.id === pendingId
              ? { ...tn, id: `turn-${n}`, index: n }
              : tn,
          );
          if (!merged.turns.some((tn) => tn.id === `turn-${n}`)) {
            merged.turns = [...merged.turns, { ...pending, id: `turn-${n}`, index: n }];
          }
        }
        return merged;
      });
    } catch (e) {
      setSession((s) =>
        s ? { ...s, turns: s.turns.filter((x) => x.id !== pendingId) } : s,
      );
      throw e;
    }
  };

  const act = async (fn: () => Promise<Session>) => {
    setBusy(true);
    try {
      const next = await fn();
      setSession((s) => (s ? mergeSessionMeta(s, next) : next));
      if (next.hasChanges || next.delivery) {
        setChanges(await api.listChanges(id).catch(() => changes));
      }
    } catch (e) {
      setError(isApiError(e) ? e : null);
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return <div className="empty"><Spinner /> {t("common.loading")}</div>;
  }
  if (!session) {
    return (
      <div>
        <ErrorNotice
          error={error ?? new ApiError("not_found", "not found", { subcode: "not_found", httpStatus: 404 })}
          onRetry={load}
        />
        <div className="empty">
          <div>{t("session.not_found")}</div>
          <Link to="/sessions">{t("common.back")}</Link>
        </div>
      </div>
    );
  }

  const live = session.phase === "queued" || session.phase === "starting";
  const runningLike = live || session.phase === "running";
  const tabs: { key: Tab; label: string; badge?: number }[] = [
    { key: "conversation", label: t("session.conversation") },
    { key: "activity", label: t("session.activity") },
    ...(session.hasChanges || changes.length > 0 || session.delivery
      ? [{ key: "changes" as Tab, label: t("session.changes"), badge: changes.length }]
      : []),
  ];

  return (
    <div>
      <div className="session-head">
        <h1>{session.title}</h1>
        <StatusPill phase={session.phase} endReason={session.endReason} />
      </div>
      <div className="session-meta-row">
        <ProviderBadge provider={session.provider} model={session.model} />
        {session.repo && <span className="mono">{session.repo.name}</span>}
        {session.effort && session.effort !== "auto" && (
          <span>{t("detail.effort")}: {session.effort}</span>
        )}
        {session.phase === "failed" && (
          <button
            className="btn btn-sm"
            disabled={busy}
            onClick={() => void act(() => api.retrySession(id))}
            data-testid="retry-btn"
          >
            {t("session.retry")}
          </button>
        )}
        {session.hasChanges && session.delivery?.status !== "delivered" && (
          <button
            className="btn btn-sm"
            disabled={busy}
            onClick={() => void act(() => api.deliverSession(id))}
            data-testid="deliver-btn"
          >
            {busy ? t("session.delivering") : t("session.deliver")}
          </button>
        )}
      </div>

      {live && (
        <div className={`phase-banner ${session.phase}`} data-testid="phase-banner">
          <Spinner size={14} />
          {t("session.waiting")} — {t(`phase.${session.phase}` as const)}
        </div>
      )}
      {session.phase === "failed" && session.error && (
        <ErrorNotice
          error={
            new ApiError("session_failed", session.error.message, {
              subcode: session.error.code,
              retryable: session.error.retryable,
            })
          }
          provider={session.provider ?? undefined}
          onRetry={() => void act(() => api.retrySession(id))}
        />
      )}
      <ReconnectBanner retryInMs={retryInMs} reconnected={reconnected} />
      <ErrorNotice error={error} onRetry={load} onDismiss={() => setError(null)} />

      <div className="detail-grid">
        <div>
          <div className="tabs" role="tablist">
            {tabs.map((tb) => (
              <button
                key={tb.key}
                role="tab"
                aria-selected={tab === tb.key}
                className={tab === tb.key ? "active" : ""}
                onClick={() => setTab(tb.key)}
                data-testid={`tab-${tb.key}`}
              >
                {tb.label}
                {tb.badge ? <span className="badge">{tb.badge}</span> : null}
              </button>
            ))}
          </div>
          {tab === "conversation" && <Conversation turns={session.turns} />}
          {tab === "activity" && <ActivityTimeline turns={session.turns} extra={feed} />}
          {tab === "changes" && <ChangesPanel changes={changes} />}
          <div ref={bottomRef} />
          <FollowUp
            phase={session.phase}
            onSend={sendFollowUp}
            onStop={runningLike ? () => void act(() => api.stopSession(id)) : undefined}
          />
        </div>
        <SessionMeta session={session} />
      </div>
    </div>
  );
}

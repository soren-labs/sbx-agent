import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { ApiError, isApiError } from "../api";
import type {
  Session,
  SessionChange,
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

export function SessionDetailPage() {
  const { t } = useI18n();
  const api = useApi();
  const { id = "" } = useParams();
  const location = useLocation();
  const optimistic = (location.state as { session?: Session } | null)?.session;

  const [session, setSession] = useState<Session | null>(optimistic ?? null);
  const [changes, setChanges] = useState<SessionChange[]>([]);
  const [loading, setLoading] = useState(!optimistic);
  const [error, setError] = useState<ApiError | null>(null);
  const [tab, setTab] = useState<Tab>("conversation");
  const [retryInMs, setRetryInMs] = useState<number | null>(null);
  const [reconnected, setReconnected] = useState(false);
  const seqRef = useRef(10_000);
  const bottomRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      const s = await api.getSession(id);
      setSession(s);
      setError(null);
      if (s.hasChanges) {
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

  // Live subscription: phases, activity items, turn updates, stream drops.
  useEffect(() => {
    if (!id) return;
    const unsub = api.subscribe(id, {
      onPhase: (phase) =>
        setSession((s) => (s ? { ...s, phase } : s)),
      onSession: (s) => setSession(s),
      onTurn: (turn) =>
        setSession((s) => {
          if (!s) return s;
          const idx = s.turns.findIndex((x) => x.id === turn.id);
          const turns =
            idx >= 0
              ? s.turns.map((x, i) => (i === idx ? turn : x))
              : [...s.turns, turn];
          return { ...s, turns };
        }),
      onActivity: (item) =>
        setSession((s) => {
          if (!s) return s;
          const turns = s.turns.map((tn) =>
            tn.id === item.turnId
              ? { ...tn, activity: [...tn.activity, item] }
              : tn,
          );
          const stray = !item.turnId || !s.turns.some((tn) => tn.id === item.turnId);
          if (stray && item.kind === "status" && item.status) {
            // session-level status frame
            const next = { ...s };
            next.phase = (
              { queued: "queued", starting: "starting", running: "running" } as const
            )[item.status] ?? s.phase;
            return next;
          }
          if (stray) {
            const last = turns[turns.length - 1];
            if (last) {
              last.activity = [...last.activity, { ...item, turnId: last.id }];
            }
          }
          return { ...s, turns };
        }),
      onDisconnect: (ms) => {
        setReconnected(false);
        setRetryInMs(ms);
      },
      onReconnect: () => {
        setRetryInMs(null);
        setReconnected(true);
        setTimeout(() => setReconnected(false), 3000);
      },
      onError: (e) => setError(e),
    });
    return unsub;
  }, [api, id]);

  // Follow the conversation tail on new activity.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [session?.turns.length, session?.updatedAt]);

  const sendFollowUp = async (text: string) => {
    // Optimistic turn so the prompt lands instantly.
    const pending = {
      id: `pending-${++seqRef.current}`,
      index: (session?.turns.length ?? 0) + 1,
      prompt: text,
      status: "queued" as const,
      createdAt: new Date().toISOString(),
      startedAt: null,
      finishedAt: null,
      result: null,
      error: null,
      activity: [],
    };
    setSession((s) => (s ? { ...s, turns: [...s.turns, pending] } : s));
    try {
      await api.sendFollowUp(id, text);
    } catch (e) {
      setSession((s) =>
        s ? { ...s, turns: s.turns.filter((x) => x.id !== pending.id) } : s,
      );
      throw e;
    }
  };

  const stop = async () => {
    try {
      await api.stopSession(id);
      await load();
    } catch (e) {
      setError(isApiError(e) ? e : null);
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
    ...(session.hasChanges || changes.length > 0
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
        {session.effort && <span>{t("detail.effort")}: {session.effort}</span>}
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
          provider={session.provider}
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
          {tab === "activity" && <ActivityTimeline turns={session.turns} />}
          {tab === "changes" && <ChangesPanel changes={changes} />}
          <div ref={bottomRef} />
          <FollowUp
            phase={session.phase}
            onSend={sendFollowUp}
            onStop={runningLike ? stop : undefined}
          />
        </div>
        <SessionMeta session={session} />
      </div>
    </div>
  );
}

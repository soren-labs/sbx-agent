import { useCallback, useEffect, useRef, useState } from "react";
import type { EventEnvelope } from "../../../api/types";
import { useI18n } from "../../../i18n";
import type { I18nKey } from "../../../i18n/en";
import { useApi } from "../../../state/context";
import type { SessionLiveState } from "../../../state/session-live";
import { Button } from "../../../ui/Button";
import { EmptyState } from "../../../ui/EmptyState";
import { ErrorNotice } from "../../../ui/ErrorNotice";
import { Pill } from "../../../ui/Pill";
import { Skeleton } from "../../../ui/Skeleton";
import { turnTone } from "../status";
import { formatDuration, secondsBetween } from "../worklog";

/** What happened, in words; the event type stays available as a tooltip for support. */
function describe(e: EventEnvelope): string {
  const p = e.payload ?? {};
  if (e.type.startsWith("tool.")) return [p.name, p.title].filter((x) => typeof x === "string" && x).join(" · ");
  if (e.type === "diagnostic.reported") return String(p.message ?? p.category ?? "");
  if (e.type === "turn.failed" || e.type === "delivery.failed") return String(p.message ?? p.code ?? p.reason ?? "");
  if (e.type === "turn.preparing") return String(p.reason ?? "").replaceAll("_", " ");
  if (e.type === "execution.started") return String(p.cli_version ?? "");
  if (e.type === "runtime.evidence_rejected") return [p.rejected_type, p.error].filter(Boolean).join(" · ");
  return "";
}

const tone = (type: string) =>
  /failed|unavailable|blocked|interrupted/.test(type) ? "err" : /succeeded|ready|bound|merged/.test(type) ? "ok" : "dim";

function time(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Committed events before the live stream: one bounded page at a time, oldest last. */
const HISTORY_PAGE = 100;

/** Events by seq, newest first; history pages and the live stream overlap and are deduped. */
export function mergeEvents(...lists: EventEnvelope[][]): EventEnvelope[] {
  const bySeq = new Map<number, EventEnvelope>();
  for (const list of lists) for (const e of list) bySeq.set(e.seq, e);
  return [...bySeq.values()].sort((a, b) => b.seq - a.seq);
}

export function ActivityTab({ sessionId, state }: { sessionId: string; state: SessionLiveState }) {
  const { t } = useI18n();
  const api = useApi();
  const [stream, setStream] = useState(false);
  const [history, setHistory] = useState<EventEnvelope[]>([]);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [historyError, setHistoryError] = useState<unknown>(null);
  // The snapshot's watermark is where the live stream starts: history ends there.
  const start = useRef(state.watermark + 1);

  const load = useCallback(
    async (before: number) => {
      setLoading(true);
      setHistoryError(null);
      try {
        const page = await api.sessions.eventsBefore(sessionId, before, HISTORY_PAGE);
        setHistory((h) => mergeEvents(h, page.items));
        setNextBefore(page.next_before);
      } catch (e) {
        setHistoryError(e);
      } finally {
        setLoading(false);
      }
    },
    [api, sessionId],
  );
  useEffect(() => {
    setHistory([]);
    setNextBefore(null);
    void load(start.current);
  }, [load]);

  // Per-chunk text updates are noise in a timeline; they stay one toggle away.
  const events = mergeEvents(history, state.events).filter((e) => stream || !/^(message\.part_|tool\.updated)/.test(e.type));
  return (
    <div className="activity">
      <h3 className="ov-card__title">{t("activity.turns")}</h3>
      {!state.turns.length ? <EmptyState title={t("activity.no_turns")} /> : null}
      <ul className="activity-turns">
        {state.turns.map((turn) => {
          const took = secondsBetween(turn.started_at ?? turn.created_at, turn.finished_at);
          const key = `turn.${turn.state}` as I18nKey;
          return (
            <li key={turn.id}>
              <span className="activity-turn-n">{t("conv.turn", { n: turn.ordinal })}</span>
              <Pill tone={turnTone(turn.state)}>{t(key) === key ? turn.state : t(key)}</Pill>
              {took !== null ? <span className="faint">{formatDuration(took)}</span> : null}
              {turn.error ? <span className="activity-error">{turn.error.message || turn.error.code}</span> : null}
              {turn.evidence_complete === false ? <span className="faint">{t("activity.partial_evidence")}</span> : null}
            </li>
          );
        })}
      </ul>
      <div className="activity-head">
        <h3 className="ov-card__title">{t("activity.events")}</h3>
        <label className="small check">
          <input type="checkbox" checked={stream} onChange={(e) => setStream(e.target.checked)} />
          <span>{t("activity.show_stream")}</span>
        </label>
      </div>
      {!events.length && !loading ? <EmptyState title={t("activity.no_events")} /> : null}
      <ol className="timeline" aria-label={t("activity.events")}>
        {events.map((e) => {
          const key = `event.${e.type}` as I18nKey;
          const label = t(key) === key ? e.type.replaceAll(/[._]/g, " ") : t(key);
          const detail = describe(e);
          return (
            <li key={e.id} className={`timeline-item tone-${tone(e.type)}`} title={`${t("activity.raw")} #${e.seq} · ${e.type}`}>
              <time>{time(e.recorded_at)}</time>
              <span className="timeline-dot" aria-hidden="true" />
              <span className="timeline-text">
                <strong>{label}</strong>
                {detail ? <span className="timeline-detail">{detail}</span> : null}
              </span>
            </li>
          );
        })}
      </ol>
      {loading ? <Skeleton height={40} /> : null}
      <ErrorNotice error={historyError} onRetry={() => void load(nextBefore ?? start.current)} />
      {nextBefore !== null && !loading ? (
        <Button size="sm" variant="secondary" onClick={() => void load(nextBefore)}>
          {t("activity.load_earlier")}
        </Button>
      ) : null}
    </div>
  );
}

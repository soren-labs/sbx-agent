import { useState } from "react";
import type { EventEnvelope } from "../../api/types";
import { Empty, Pill } from "../../components/ui";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import type { SessionLiveState } from "../../state/session-live";
import { turnTone } from "./Conversation";
import { formatDuration, secondsBetween } from "./worklog";

/** What happened, in words; the event type stays available as a tooltip for support. */
function describe(e: EventEnvelope): string {
  const p = e.payload ?? {};
  if (e.type.startsWith("tool.")) return [p.name, p.title].filter((x) => typeof x === "string" && x).join(" · ");
  if (e.type === "diagnostic.reported") return String(p.message ?? p.category ?? "");
  if (e.type === "turn.failed" || e.type === "delivery.failed") return String(p.message ?? p.code ?? p.reason ?? "");
  if (e.type === "turn.preparing") return String(p.reason ?? "").replaceAll("_", " ");
  if (e.type === "execution.started") return String(p.cli_version ?? "");
  return "";
}

const tone = (type: string) =>
  /failed|unavailable|blocked|interrupted/.test(type) ? "err" : /succeeded|ready|bound|merged/.test(type) ? "ok" : "dim";

function time(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function ActivityTab({ state }: { state: SessionLiveState }) {
  const { t } = useI18n();
  const [stream, setStream] = useState(false);
  // Per-chunk text updates are noise in a timeline; they stay one toggle away.
  const events = [...state.events]
    .filter((e) => stream || !/^(message\.part_|tool\.updated)/.test(e.type))
    .reverse()
    .slice(0, 300);
  return (
    <div className="activity">
      <h2>{t("activity.turns")}</h2>
      {!state.turns.length ? <Empty>{t("activity.no_turns")}</Empty> : null}
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
        <h2>{t("activity.events")}</h2>
        <label className="wl-note-toggle">
          <input type="checkbox" checked={stream} onChange={(e) => setStream(e.target.checked)} />
          <span>{t("activity.show_stream")}</span>
        </label>
      </div>
      {!events.length ? <Empty>{t("activity.no_events")}</Empty> : null}
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
    </div>
  );
}

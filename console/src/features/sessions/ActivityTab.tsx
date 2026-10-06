import type { EventEnvelope } from "../../api/types";
import { Empty, Pill, when } from "../../components/ui";
import { useI18n } from "../../i18n";
import type { SessionLiveState } from "../../state/session-live";
import { turnTone } from "./Conversation";

function summary(e: EventEnvelope): string {
  const p = e.payload ?? {};
  if (e.type.startsWith("tool.")) return `${String(p.name ?? "")} ${String(p.status ?? "")} ${String(p.title ?? "")}`.trim();
  if (e.type === "diagnostic.reported") return String(p.message ?? p.code ?? "");
  if (e.type.startsWith("message.part_")) return `${String(p.kind ?? "")} r${String(p.revision ?? "")}`;
  return "";
}

export function ActivityTab({ state }: { state: SessionLiveState }) {
  const { t } = useI18n();
  const events = [...state.events].reverse().slice(0, 200);
  return (
    <div>
      <h2>{t("activity.turns")}</h2>
      {!state.turns.length ? <Empty>{t("activity.no_turns")}</Empty> : null}
      <ul className="plain-list">
        {state.turns.map((turn) => (
          <li key={turn.id} className="row wrap small">
            <span>{t("conv.turn", { n: turn.ordinal })}</span>
            <Pill tone={turnTone(turn.state)}>{turn.state}</Pill>
            {turn.reason ? <span className="faint">{turn.reason}</span> : null}
            {turn.error ? <span>{turn.error.message || turn.error.code}</span> : null}
            {turn.evidence_complete === false ? <span className="faint">{t("activity.partial_evidence")}</span> : null}
          </li>
        ))}
      </ul>
      <h2>{t("activity.events")}</h2>
      {!events.length ? <Empty>{t("activity.no_events")}</Empty> : null}
      <ol className="event-feed small" reversed aria-label={t("activity.events")}>
        {events.map((e) => (
          <li key={e.id}>
            <span className="faint mono">#{e.seq}</span> <code>{e.type}</code> <span className="muted">{summary(e)}</span>{" "}
            <span className="faint">{when(e.recorded_at)}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

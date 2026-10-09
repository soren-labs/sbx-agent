import { Pill, type Tone } from "../../components/ui";
import { useI18n } from "../../i18n";
import type { Session } from "../../api/types";
import type { I18nKey } from "../../i18n/en";

const ACTIVITY_TONE: Record<string, Tone> = {
  running: "run",
  queued: "warn",
  preparing: "warn",
  failed: "err",
  idle: "ok",
};

export const leaseTone = (s: string | null): Tone =>
  s === "ready" ? "ok" : s === null || s === "released" ? "dim" : s === "quarantined" || s === "unavailable" ? "err" : "warn";
export const availabilityTone = (a: string): Tone =>
  a === "live" || a === "available" || a === "ready" ? "ok" : a === "lost" || a === "unavailable" ? "err" : "dim";

/** Conversation activity: what the server says the Session's Turns are doing. */
export function ActivityPill({ activity }: { activity: string }) {
  const { t } = useI18n();
  const key = `activity.${activity}` as I18nKey;
  const label = t(key);
  // Unknown server states are shown readably rather than hidden or mislabelled.
  return <Pill tone={ACTIVITY_TONE[activity] ?? "dim"}>{label === key ? activity.replaceAll("_", " ") : label}</Pill>;
}

/** Compute availability, shown separately from conversation outcome. */
export function AvailabilityPills({ session }: { session: Session }) {
  const { t } = useI18n();
  const lease = session.executor.lease_state;
  const wt = session.worktree;
  return (
    <span className="row wrap" aria-label={t("session.availability")}>
      <Pill tone={leaseTone(lease)} title={t("session.compute")}>
        {t("session.compute")}: {lease ?? t("session.no_lease")}
      </Pill>
      {wt ? (
        <Pill tone={availabilityTone(wt.availability)} title={t("session.worktree")}>
          {t("session.worktree")}: {wt.availability}
        </Pill>
      ) : null}
    </span>
  );
}

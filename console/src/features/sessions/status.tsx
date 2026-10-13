import { Pill, type Tone } from "../../ui/Pill";
import { useI18n } from "../../i18n";
import type { Session } from "../../api/types";

export const turnTone = (s: string): Tone =>
  s === "succeeded" ? "ok" : s === "failed" || s === "interrupted" ? "err" : s === "cancelled" ? "dim" : s === "running" ? "run" : "warn";

export const leaseTone = (s: string | null): Tone =>
  s === "ready" ? "ok" : s === null || s === "released" ? "dim" : s === "quarantined" || s === "unavailable" ? "err" : "warn";

export const availabilityTone = (a: string): Tone =>
  a === "live" || a === "available" || a === "ready" ? "ok" : a === "lost" || a === "unavailable" ? "err" : "dim";

/** Compute availability, shown separately from conversation outcome. */
export function AvailabilityPills({ session }: { session: Session }) {
  const { t } = useI18n();
  const lease = session.executor.lease_state;
  const wt = session.worktree;
  return (
    <span className="pill-row" aria-label={t("session.availability")}>
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

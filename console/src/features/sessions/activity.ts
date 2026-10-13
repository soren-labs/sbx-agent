import type { Session } from "../../api/types";
import type { Tone } from "../../ui/Pill";
import type { I18nKey } from "../../i18n/en";

/**
 * Sidebar/list status: blue while working, orange while it waits for the user,
 * red on failures, nothing otherwise.
 */
export function activityTone(s: Session): Tone | null {
  const a = `${s.activity}`.toLowerCase();
  if (a.includes("fail") || a.includes("error")) return "err";
  if (s.active_turn_id || a === "running" || a === "starting" || a === "queued") return "info";
  if (a.includes("wait") || a.includes("await")) return "warn";
  return null;
}

export function activityLabelKey(s: Session): I18nKey {
  if (s.lifecycle === "closed") return "status.closed";
  if (s.lifecycle === "archived") return "status.archived";
  const a = `${s.activity}`.toLowerCase();
  if (a.includes("fail") || a.includes("error") || a === "attention") return "status.attention";
  if (a.includes("wait") || a.includes("await")) return "status.waiting";
  if (a === "running" || a === "starting" || s.active_turn_id) return "status.running";
  if (a === "queued") return "status.queued";
  return "status.idle";
}

import type { DeliveryMode, EffortLevel } from "../api/types";

/** User-level session defaults, persisted locally (Settings page). */
export interface SessionDefaults {
  effort: EffortLevel | "auto";
  idleTimeoutS: number | "";
  delivery: DeliveryMode;
}

const KEY = "sbx.console.defaults";

export function loadDefaults(): SessionDefaults {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? "{}") as Partial<SessionDefaults>;
    return {
      effort: raw.effort ?? "auto",
      idleTimeoutS: raw.idleTimeoutS ?? "",
      delivery: raw.delivery ?? "none",
    };
  } catch {
    return { effort: "auto", idleTimeoutS: "", delivery: "none" };
  }
}

export function saveDefaults(d: SessionDefaults) {
  localStorage.setItem(KEY, JSON.stringify(d));
}

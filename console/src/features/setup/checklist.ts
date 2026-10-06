import type { Connection, ConnectionKind, Me } from "../../api/types";

export type SetupId = "account" | ConnectionKind;
/** missing: nothing configured · pending: configured, awaiting server verification · attention: server reports a problem */
export type SetupStatus = "missing" | "pending" | "attention" | "ready";

export interface SetupItem {
  id: SetupId;
  required: boolean;
  status: SetupStatus;
  connection?: Connection;
}

export interface SetupSummary {
  items: SetupItem[];
  /** All required items are ready. Codex is never required. */
  complete: boolean;
  next: SetupItem | null;
}

const REQUIRED: SetupId[] = ["account", "modal", "github", "opencode_zen"];
const ORDER: SetupId[] = [...REQUIRED, "codex"];

function statusOf(conns: Connection[]): { status: SetupStatus; connection?: Connection } {
  const live = conns.filter((c) => c.state === "configured");
  if (!live.length) return { status: "missing" };
  const ready = live.find((c) => c.health === "ready");
  if (ready) return { status: "ready", connection: ready };
  const pending = live.find((c) => c.health === "unverified" || c.health === "verifying");
  if (pending) return { status: "pending", connection: pending };
  return { status: "attention", connection: live[0] };
}

/** Pure projection of server-provided identity/connection fields; makes no readiness decisions. */
export function computeSetup(me: Me | null, connections: Connection[]): SetupSummary {
  const items: SetupItem[] = ORDER.map((id) => {
    const required = REQUIRED.includes(id);
    if (id === "account") {
      return { id, required, status: me?.user.email_verified ? "ready" : "attention" };
    }
    return { id, required, ...statusOf(connections.filter((c) => c.kind === id)) };
  });
  const next = items.find((i) => i.required && i.status !== "ready") ?? null;
  return { items, complete: next === null, next };
}

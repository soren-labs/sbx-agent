import { setToken } from "./http";

const API_BASE = (
  (import.meta.env.VITE_API_BASE as string | undefined) ?? ""
).replace(/\/+$/, "");

/**
 * SOR-266 auth boot: `sbx open` hands the browser a one-time grant ticket
 * (`#/connect?grant=<ticket>` on the legacy hash URL, or `?grant=`). Read it
 * before the app renders, redeem it once via `POST /v1/console/exchange`,
 * store the minted key, and scrub the URL so the ticket never survives in
 * history or reach the server twice.
 */
export async function bootstrapGrant(): Promise<boolean> {
  const grant = readGrant();
  if (!grant) return false;
  // Scrub immediately — the ticket is single-use regardless of outcome.
  window.history.replaceState(null, "", "/");
  try {
    const res = await fetch(`${API_BASE}/v1/console/exchange`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ grant }),
    });
    if (!res.ok) return false;
    const body = (await res.json()) as { key?: unknown };
    if (typeof body?.key !== "string" || !body.key) return false;
    setToken(body.key);
    return true;
  } catch {
    return false;
  }
}

function readGrant(): string {
  const query = new URLSearchParams(window.location.search).get("grant");
  if (query) return query;
  const hash = window.location.hash; // "#/connect?grant=<ticket>" or "#grant="
  const q = hash.indexOf("?");
  const params =
    q >= 0
      ? new URLSearchParams(hash.slice(q + 1))
      : new URLSearchParams(hash.replace(/^#/, ""));
  return params.get("grant") ?? "";
}

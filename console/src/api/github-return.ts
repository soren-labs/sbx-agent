import { getToken } from "./http";
/** Adapt the existing installation callback to the current product route. */
export async function handleGithubReturn() {
  const query = new URLSearchParams(location.search);
  const hash = location.hash.match(/^#\/admin\/github\??(.*)$/);
  if (hash) {
    const old = new URLSearchParams(hash[1]);
    const outcome = old.get("broker_error")
      ? `broker_error=${encodeURIComponent(old.get("broker_error")!)}`
      : "broker=connected";
    history.replaceState(null, "", `/integrations?${outcome}`);
    return;
  }
  const installation = query.get("installation_id"),
    state = query.get("state");
  if (!installation || !state) return;
  // Remove the single-use authorization state from browser history before sending it.
  history.replaceState(null, "", "/integrations");
  try {
    const base = String(import.meta.env.VITE_API_BASE ?? "").replace(
      /\/+$/,
      "",
    );
    const r = await fetch(base + "/v1/github/app/authorize/callback", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${getToken()}`,
      },
      body: JSON.stringify({ installation_id: Number(installation), state }),
    });
    history.replaceState(
      null,
      "",
      `/integrations?${r.ok ? "broker=connected" : "broker_error=authorization_failed"}`,
    );
  } catch {
    history.replaceState(
      null,
      "",
      "/integrations?broker_error=connection_failed",
    );
  }
}

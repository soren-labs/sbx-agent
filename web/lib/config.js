/** Runtime config. Override on `window` before this module loads, or via same-origin. */

export function apiBase() {
  const raw = window.SBX_API_BASE;
  if (typeof raw === "string" && raw.trim()) {
    return raw.replace(/\/+$/, "");
  }
  return "";
}

function isLocalHost() {
  const h = window.location.hostname;
  return h === "localhost" || h === "127.0.0.1";
}

export function apiUser() {
  if (typeof window.SBX_API_USER === "string") return window.SBX_API_USER;
  // Local mock / Playwright only. Production (sbx.sorenforge.com) uses the
  // Cloudflare edge proxy so the browser never sees Basic credentials.
  return isLocalHost() ? "sbx" : "";
}

export function apiPassword() {
  if (typeof window.SBX_API_PASSWORD === "string") return window.SBX_API_PASSWORD;
  return isLocalHost() ? "sbx" : "";
}

export function origin() {
  const base = apiBase();
  if (base) return base;
  return window.location.origin;
}

export function apiUrl(path) {
  const p = path.startsWith("/") ? path : `/${path}`;
  const base = apiBase();
  return `${base}${p}`;
}

export function eventsUrl(sessionId) {
  const u = new URL(`/api/sessions/${encodeURIComponent(sessionId)}/events`, origin());
  const user = apiUser();
  const password = apiPassword();
  if (user) {
    u.username = user;
    u.password = password;
  }
  return u.toString();
}

export function authHeader() {
  const user = apiUser();
  if (!user) return {};
  const token = btoa(`${user}:${apiPassword()}`);
  return { Authorization: `Basic ${token}` };
}

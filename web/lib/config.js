/**
 * Connection settings: control-plane URL + `sbx_` API key.
 *
 * The key lives in sessionStorage by default (gone when the tab closes);
 * "remember on this device" moves it to localStorage. It is only ever sent
 * as `Authorization: Bearer` to the configured control plane.
 */

const STORE_KEY = "sbx.console.connection";

function read(storage) {
  try {
    const raw = storage.getItem(STORE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

/** Default API origin: `window.SBX_API_BASE` (optional) or same origin. */
export function defaultBaseUrl() {
  const raw = window.SBX_API_BASE;
  return typeof raw === "string" && raw.trim() ? raw.trim().replace(/\/+$/, "") : "";
}

let cached = null;

export function getConnection() {
  if (cached) return cached;
  const stored = read(sessionStorage) || read(localStorage);
  cached = {
    baseUrl: stored?.baseUrl ?? defaultBaseUrl(),
    apiKey: stored?.apiKey ?? "",
    remember: Boolean(stored?.remember),
    identity: stored?.identity ?? null,
  };
  return cached;
}

export function saveConnection({ baseUrl, apiKey, remember, identity }) {
  const value = {
    baseUrl: (baseUrl || "").trim().replace(/\/+$/, ""),
    apiKey: (apiKey || "").trim(),
    remember: Boolean(remember),
    identity: identity ?? null,
  };
  sessionStorage.removeItem(STORE_KEY);
  localStorage.removeItem(STORE_KEY);
  (value.remember ? localStorage : sessionStorage).setItem(STORE_KEY, JSON.stringify(value));
  cached = value;
  return value;
}

export function clearConnection() {
  sessionStorage.removeItem(STORE_KEY);
  localStorage.removeItem(STORE_KEY);
  cached = null;
}

export function isConnected() {
  return Boolean(getConnection().apiKey);
}

export function hasScope(scope) {
  const scopes = getConnection().identity?.scopes || [];
  return scopes.includes(scope);
}

/** Human label for the control plane the console talks to. */
export function planeLabel() {
  const base = getConnection().baseUrl;
  try {
    return new URL(base || window.location.origin).host;
  } catch {
    return base || window.location.host;
  }
}

import { apiUrl, authHeader } from "./config.js";
import { friendlyHttpError } from "./format.js";

export class ApiError extends Error {
  constructor(status, body) {
    super(friendlyHttpError(status, body));
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function parseBody(res) {
  const text = await res.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { error: text };
  }
}

async function request(path, options = {}) {
  const headers = {
    Accept: "application/json",
    ...authHeader(),
    ...(options.headers || {}),
  };
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  let res;
  try {
    res = await fetch(apiUrl(path), { ...options, headers });
  } catch (err) {
    throw new ApiError(0, { error: err instanceof Error ? err.message : "network_error", code: 0 });
  }
  const body = await parseBody(res);
  if (!res.ok) {
    throw new ApiError(res.status, body);
  }
  return body;
}

export function listSessions() {
  return request("/api/sessions");
}

export function getSession(id) {
  return request(`/api/sessions/${encodeURIComponent(id)}`);
}

export function createSession({ title, model } = {}) {
  return request("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ title, model }),
  });
}

export function postMessage(id, text) {
  return request(`/api/sessions/${encodeURIComponent(id)}/messages`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}

export function stopSession(id) {
  return request(`/api/sessions/${encodeURIComponent(id)}/stop`, { method: "POST" });
}

export function closeSession(id) {
  return request(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
}

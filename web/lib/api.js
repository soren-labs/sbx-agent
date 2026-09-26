import { getConnection } from "./config.js";

/** Error from the /v1 API: `{error: {code, message, retry_after?}}`. */
export class ApiError extends Error {
  constructor({ status, code, message, retryAfter = null, body = null }) {
    super(message || code || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code || (status === 0 ? "network_error" : `http_${status}`);
    this.retryAfter = retryAfter;
    this.body = body;
  }
}

export function apiUrl(path, query) {
  const base = getConnection().baseUrl || window.location.origin;
  const url = new URL(path, base.endsWith("/") ? base : `${base}/`);
  // `new URL("/v1/x", "https://h/prefix/")` drops the prefix; keep it.
  if (getConnection().baseUrl) {
    const prefix = new URL(base).pathname.replace(/\/+$/, "");
    url.pathname = `${prefix}${path}`;
  }
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value != null && value !== "") url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

export function authHeaders(key = getConnection().apiKey) {
  return key ? { Authorization: `Bearer ${key}` } : {};
}

async function parseError(res) {
  let body = null;
  const text = await res.text().catch(() => "");
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = { raw: text };
  }
  const err = body?.error;
  if (err && typeof err === "object") {
    return new ApiError({
      status: res.status,
      code: err.code,
      message: err.message,
      retryAfter: err.retry_after ?? null,
      body,
    });
  }
  // FastAPI validation errors: {detail: [...]}
  if (Array.isArray(body?.detail)) {
    const first = body.detail[0] || {};
    const where = Array.isArray(first.loc) ? first.loc.slice(1).join(".") : "";
    return new ApiError({
      status: res.status,
      code: "invalid_request",
      message: where ? `${where}: ${first.msg}` : first.msg,
      body,
    });
  }
  return new ApiError({
    status: res.status,
    code: typeof body?.detail === "string" ? body.detail : undefined,
    body,
  });
}

export async function request(
  method,
  path,
  { query, body, key, raw = false, signal, headers: extra } = {},
) {
  const headers = { Accept: "application/json", ...authHeaders(key), ...(extra || {}) };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(apiUrl(path, query), {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
      cache: "no-store",
    });
  } catch (err) {
    if (err?.name === "AbortError") throw err;
    throw new ApiError({ status: 0, code: "network_error", message: String(err?.message || err) });
  }
  if (!res.ok) {
    const error = await parseError(res);
    if (res.status === 401 && key === undefined) {
      window.dispatchEvent(new CustomEvent("sbx:unauthorized", { detail: error }));
    }
    throw error;
  }
  if (raw) return res;
  if (res.status === 204) return null;
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

const enc = encodeURIComponent;

export const api = {
  me: (key) => request("GET", "/v1/me", { key }),
  models: () => request("GET", "/v1/models"),

  // Tasks: the canonical product surface — create/preflight resolve
  // provider, account, model and the repo pin server-side.
  listTasks: () => request("GET", "/v1/tasks"),
  getTask: (id) => request("GET", `/v1/tasks/${enc(id)}`),
  createTask: (body, idempotencyKey) =>
    request("POST", "/v1/tasks", {
      body,
      headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
    }),
  taskPreflight: (body) => request("POST", "/v1/tasks/preflight", { body }),
  cancelTask: (id) => request("POST", `/v1/tasks/${enc(id)}/cancel`),
  retryTask: (id, body) => request("POST", `/v1/tasks/${enc(id)}/retry`, { body }),
  deliverTask: (id) => request("POST", `/v1/tasks/${enc(id)}/delivery`),
  listTaskRuns: (id) => request("GET", `/v1/tasks/${enc(id)}/runs`),

  listAgents: (query) => request("GET", "/v1/agents", { query }),
  agentsSummary: (query) => request("GET", "/v1/agents/summary", { query }),
  getAgent: (id) => request("GET", `/v1/agents/${enc(id)}`),
  createAgent: (body, idempotencyKey) =>
    request("POST", "/v1/agents", {
      body,
      headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
    }),
  closeAgent: (id) => request("DELETE", `/v1/agents/${enc(id)}`),
  usage: (id) => request("GET", `/v1/agents/${enc(id)}/usage`),

  listRuns: (id) => request("GET", `/v1/agents/${enc(id)}/runs`),
  getRun: (id, runId) => request("GET", `/v1/agents/${enc(id)}/runs/${enc(runId)}`),
  createRun: (id, body) => request("POST", `/v1/agents/${enc(id)}/runs`, { body }),
  cancelRun: (id, runId) => request("POST", `/v1/agents/${enc(id)}/runs/${enc(runId)}/cancel`),
  streamPath: (id, runId) => `/v1/agents/${enc(id)}/runs/${enc(runId)}/stream`,

  workspace: (id) => request("GET", `/v1/agents/${enc(id)}/workspace`),
  review: (id, body) => request("POST", `/v1/agents/${enc(id)}/workspace/review`, { body }),
  publish: (id) => request("POST", `/v1/agents/${enc(id)}/git/publish`),
  merge: (id) => request("POST", `/v1/agents/${enc(id)}/git/merge`),
  handoff: (id, body) => request("POST", `/v1/agents/${enc(id)}/handoff`, { body }),

  createArtifact: (id, body) => request("POST", `/v1/agents/${enc(id)}/artifacts`, { body }),
  listArtifacts: (query) => request("GET", "/v1/artifacts", { query }),
  getArtifact: (artifactId) => request("GET", `/v1/artifacts/${enc(artifactId)}`),
  downloadArtifact: (artifactId, member) =>
    request("GET", `/v1/artifacts/${enc(artifactId)}/download`, { query: { member }, raw: true }),

  workflow: (workflowId) => request("GET", `/v1/workflows/${enc(workflowId)}`),
  closeWorkflow: (workflowId) => request("DELETE", `/v1/workflows/${enc(workflowId)}`),

  listAccounts: (query) => request("GET", "/v1/accounts", { query }),
  createAccount: (body) => request("POST", "/v1/accounts", { body }),
  deleteAccount: (id) => request("DELETE", `/v1/accounts/${enc(id)}`),
  verifyAccount: (id) => request("POST", `/v1/accounts/${enc(id)}/verify`),

  listKeys: () => request("GET", "/v1/api-keys"),
  createKey: (body) => request("POST", "/v1/api-keys", { body }),
  revokeKey: (id) => request("DELETE", `/v1/api-keys/${enc(id)}`),

  // SOR-211: redeem a one-time `sbx open` grant ticket for a minted key.
  // Unauthenticated by design — key:"" suppresses any stored Bearer.
  exchangeGrant: (grant) =>
    request("POST", "/v1/console/exchange", { body: { grant }, key: "" }),

  githubStatus: () => request("GET", "/v1/github/app"),
  githubAuthorize: () => request("POST", "/v1/github/app/authorize"),
  githubCallback: (body) => request("POST", "/v1/github/app/authorize/callback", { body }),
  githubSync: () => request("POST", "/v1/github/app/sync"),
  githubRevoke: (installationId) =>
    request("DELETE", `/v1/github/app/installations/${enc(installationId)}`),
  // SOR-220 default connect: {authorize_url: github.com/apps/<public app>/installations/new, mode:"broker"}
  githubInstall: () => request("POST", "/v1/github/install"),
  // SOR-220: per-deployment App registration via GitHub's manifest flow —
  // the returned {manifest, manifest_url} is form-posted by the Console.
  githubManifest: (body) => request("POST", "/v1/github/app/manifest", { body }),
  githubManifestComplete: (body) =>
    request("POST", "/v1/github/app/manifest/complete", { body }),

  // SOR-214: provider connect sessions over the canonical auth engine.
  authSessions: () => request("GET", "/v1/auth"),
  authConnect: (body) => request("POST", "/v1/auth/connect", { body }),
  connectSession: (id) => request("GET", `/v1/auth/connect/${enc(id)}`),
  connectCancel: (id) => request("POST", `/v1/auth/connect/${enc(id)}/cancel`),
  connectRetry: (id) => request("POST", `/v1/auth/connect/${enc(id)}/retry`),
};

/** Unwrap `{artifact}` (create) vs bare manifest (get) responses. */
export function unwrapArtifact(body) {
  return body?.artifact ?? body;
}

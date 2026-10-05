/**
 * Unified /api client (RFC 167 §08) — the single typed surface the console
 * talks to. No V1/V2/hosted layering. Secrets are write-only inputs; the
 * client never stores them.
 */

export interface ApiErrorShape {
  code: string;
  category: string;
  message: string;
  retryable: boolean;
  retry_after?: number;
  action?: string;
  details: Record<string, unknown>;
  request_id: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly error: ApiErrorShape;

  constructor(status: number, error: ApiErrorShape) {
    super(error.message);
    this.status = status;
    this.error = error;
  }

  get code(): string {
    return this.error.code;
  }
}

export interface User {
  id: string;
  email: string;
  display_name: string | null;
  verified: boolean;
  workspaces: string[];
}

export interface Session {
  id: string;
  workspace_id: string;
  role: string;
  title: string | null;
  labels: string[];
  lifecycle: string;
  project_version_id: string | null;
  projectless_spec: Record<string, unknown>;
  harness: { provider_id: string; model: string; effort?: string };
  linked_from_session_id: string | null;
  active_turn_id: string | null;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
}

export interface SessionDetail {
  session: Session;
  worktree: { id: string; state: string | null; generation: number } | null;
  executor: ExecutorLease | null;
  active_turn: Turn | null;
  watermark: number;
}

export interface Message {
  id: string;
  session_id: string;
  ordinal: number;
  author: { kind: string; id: string };
  role: string;
  routing: string;
  content: { text?: string } & Record<string, unknown>;
  reply_to_message_id: string | null;
  routed_turn_id: string | null;
  created_at: string;
}

export interface Turn {
  id: string;
  session_id: string;
  ordinal: number;
  state: string;
  reason: string | null;
  outcome: Record<string, unknown>;
  cancel_intent: unknown;
  created_at: string;
  completed_at: string | null;
}

export interface SessionEvent {
  seq: number;
  local_seq: number;
  id: string;
  type: string;
  session_id: string;
  turn_id: string | null;
  execution_id: string | null;
  source: string;
  payload: Record<string, unknown>;
  recorded_at: string;
}

export interface Connection {
  id: string;
  workspace_id: string;
  kind: "modal" | "github" | "opencode_zen" | "codex" | string;
  label: string | null;
  state: "configured" | "disabled" | "revoked";
  health: string;
  allowed_purposes: string[];
  external_identity: Record<string, unknown>;
  current_credential_version_id: string | null;
  revocation_epoch: number;
  created_at: string;
}

export interface ConnectionCapabilities {
  connection_id: string;
  state: string;
  health: string;
  external_identity: Record<string, unknown>;
  capability_observations: Record<string, unknown>;
}

export interface Model {
  id: string | null;
  provider_id: string | null;
  model: string;
  free: boolean | null;
  source_connection_id: string | null;
  capabilities: Record<string, unknown>;
}

export interface Project {
  id: string;
  slug: string;
  name: string;
  current_version_id: string | null;
}

export interface ProjectVersion {
  id: string;
  project_id: string;
  ordinal: number;
  repository: string | null;
  base_ref: string;
  environment: Record<string, unknown>;
  services: unknown[];
  defaults: Record<string, unknown>;
  spec_digest: string;
}

export interface ChangeSet {
  id: string;
  session_id: string;
  worktree_generation: number;
  subject_digest: string;
  source_turn_id: string | null;
  repository: string | null;
  base_sha: string | null;
  head_sha: string | null;
  capture_origin: string;
  created_at: string;
  manifest: { files?: unknown[] };
}

export interface Delivery {
  id: string;
  session_id: string;
  changeset_id: string;
  subject_digest: string;
  target: Record<string, unknown>;
  transport: string;
  ship_policy: Record<string, unknown>;
  state: string;
  effect_evidence: Record<string, unknown>;
  created_at: string;
  steps?: DeliveryStep[];
  merge_gate?: MergeGate;
  merge_requests?: MergeRequest[];
}

export interface DeliveryStep {
  id: string;
  kind: string;
  ordinal: number;
  state: string;
  expected: Record<string, unknown>;
  result: Record<string, unknown>;
}

export interface MergeGate {
  eligible: boolean;
  satisfied: string[];
  unmet: string[];
  reason: string | null;
}

export interface MergeRequest {
  id: string;
  state: string;
  expected_head_sha: string | null;
  merge_method: string;
  result: Record<string, unknown>;
}

export interface Delegation {
  id: string;
  parent_session_id: string;
  child_session_id: string;
  role: string;
  state: string;
  result_contract: Record<string, unknown>;
  input_digest: string | null;
  created_at: string;
}

export interface DelegationResult {
  id: string;
  verdict: string | null;
  validation_status: string;
  subject_digest: string | null;
  head_sha: string | null;
  value: Record<string, unknown>;
}

export interface Job {
  id: string;
  kind: string;
  state: string;
  target_family: string;
  target_id: string;
  attempts: number;
  result: Record<string, unknown>;
  last_error: { code?: string; message?: string } | null;
  created_at: string;
}

export interface ExecutorLease {
  id: string;
  backend: string;
  state: string;
  generation: number;
}

type Json = Record<string, unknown>;

function idemKey(): string {
  return `ui_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

export interface ClientOptions {
  baseUrl?: string;
  token?: string | null;
}

export class UnifiedApi {
  private base: string;
  private token: string | null;

  constructor(opts: ClientOptions = {}) {
    this.base = opts.baseUrl ?? "";
    this.token = opts.token ?? null;
  }

  setToken(token: string | null): void {
    this.token = token;
  }

  private async req<T>(
    method: string,
    path: string,
    opts: { body?: Json; idem?: boolean; query?: Record<string, string | number | undefined> } = {},
  ): Promise<T> {
    const url = new URL(this.base + path, window.location.origin);
    for (const [k, v] of Object.entries(opts.query ?? {})) {
      if (v !== undefined) url.searchParams.set(k, String(v));
    }
    const headers: Record<string, string> = {
      "content-type": "application/json",
    };
    if (this.token) headers["authorization"] = `Bearer ${this.token}`;
    if (opts.idem) headers["idempotency-key"] = idemKey();
    const resp = await fetch(url.toString(), {
      method,
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      credentials: "include",
    });
    const text = await resp.text();
    const data = text ? JSON.parse(text) : {};
    if (!resp.ok) {
      const err = (data as { error?: ApiErrorShape }).error;
      throw new ApiError(
        resp.status,
        err ?? {
          code: "internal",
          category: "internal",
          message: `http ${resp.status}`,
          retryable: true,
          details: {},
          request_id: "unknown",
        },
      );
    }
    return data as T;
  }

  private get<T>(path: string, query?: Record<string, string | number | undefined>) {
    return this.req<T>("GET", path, { query });
  }
  private post<T>(path: string, body?: Json, idem = true) {
    return this.req<T>("POST", path, { body, idem });
  }
  private patch<T>(path: string, body?: Json) {
    return this.req<T>("PATCH", path, { body });
  }
  private del<T>(path: string) {
    return this.req<T>("DELETE", path);
  }

  // ---- auth / me ---------------------------------------------------------
  register(email: string, password: string, displayName?: string) {
    return this.post<{ user_id: string; workspace_id: string }>(
      "/api/auth/register",
      { email, password, display_name: displayName },
      false,
    );
  }

  login(email: string, password: string) {
    return this.post<{ token: string; user: User }>(
      "/api/auth/login",
      { email, password },
      false,
    );
  }

  logout() {
    return this.post<{ ok: boolean }>("/api/auth/logout", {}, false);
  }

  me() {
    return this.get<User>("/api/me");
  }

  // ---- api keys ----------------------------------------------------------
  listApiKeys() {
    return this.get<{ items: { id: string; label: string | null; scopes: string[] }[] }>(
      "/api/api-keys",
    );
  }
  createApiKey(label: string, scopes = ["*"]) {
    return this.post<{ id: string; key: string }>("/api/api-keys", { label, scopes });
  }
  revokeApiKey(id: string) {
    return this.del(`/api/api-keys/${id}`);
  }

  // ---- workspaces / projects ---------------------------------------------
  listWorkspaces() {
    return this.get<{ items: { id: string; name: string; role: string | null }[] }>(
      "/api/workspaces",
    );
  }
  listProjects(workspaceId: string) {
    return this.get<{ items: Project[] }>(`/api/workspaces/${workspaceId}/projects`);
  }
  createProject(workspaceId: string, slug: string, name: string) {
    return this.post<Project>(`/api/workspaces/${workspaceId}/projects`, {
      slug,
      name,
    });
  }
  publishVersion(
    projectId: string,
    body: { repository: string; base_ref?: string; defaults?: Json; ship_policy?: Json },
  ) {
    return this.post<ProjectVersion>(`/api/projects/${projectId}/versions`, body);
  }

  // ---- connections --------------------------------------------------------
  listConnections(workspaceId: string) {
    return this.get<{ items: Connection[] }>(
      `/api/workspaces/${workspaceId}/connections`,
    );
  }
  createConnection(
    workspaceId: string,
    kind: string,
    credential: { format: string; payload: Record<string, string> },
    label?: string,
  ) {
    return this.post<Connection>(`/api/workspaces/${workspaceId}/connections`, {
      kind,
      label,
      credential,
    });
  }
  getConnection(id: string) {
    return this.get<Connection>(`/api/connections/${id}`);
  }
  updateConnection(
    id: string,
    body: { state?: "enabled" | "disabled"; label?: string; expected_version?: number },
  ) {
    return this.patch<Connection>(`/api/connections/${id}`, body);
  }
  capabilities(id: string) {
    return this.get<ConnectionCapabilities>(`/api/connections/${id}/capabilities`);
  }
  validateConnection(id: string) {
    return this.post<{ job: Job }>(`/api/connections/${id}/validations`, {});
  }
  disconnect(id: string) {
    return this.del<{ connection: Connection }>(`/api/connections/${id}`);
  }

  // ---- catalog -------------------------------------------------------------
  listModels(workspaceId: string) {
    return this.get<{ items: Model[]; default_model: Model | null }>(
      "/api/models",
      { workspace_id: workspaceId },
    );
  }
  executorBackends() {
    return this.get<{ items: { backend: string; status: string }[] }>(
      "/api/executor-backends",
    );
  }

  // ---- sessions ------------------------------------------------------------
  listSessions(
    workspaceId: string,
    params: { lifecycle?: string; role?: string; parent_session_id?: string } = {},
  ) {
    return this.get<{ items: Session[] }>(`/api/workspaces/${workspaceId}/sessions`, {
      lifecycle: params.lifecycle,
      role: params.role,
      parent_session_id: params.parent_session_id,
    });
  }
  createSession(
    workspaceId: string,
    body: {
      title?: string;
      project_version_id?: string;
      projectless_spec?: Json;
      harness: { provider_id: string; model: string; effort?: string };
      message?: { routing?: string; content: Json };
    },
  ) {
    return this.post<{ session: Session; message_id?: string; turn_id?: string }>(
      `/api/workspaces/${workspaceId}/sessions`,
      body,
    );
  }
  getSession(id: string) {
    return this.get<SessionDetail>(`/api/sessions/${id}`);
  }
  updateSession(
    id: string,
    body: { title?: string; labels?: string[]; expected_version?: number },
  ) {
    return this.patch<Session>(`/api/sessions/${id}`, body);
  }
  closeSession(id: string) {
    return this.post(`/api/sessions/${id}/closures`, {}, false);
  }
  continueSession(id: string, body: Json = {}) {
    return this.post<{ session: Session }>(`/api/sessions/${id}/continuations`, body);
  }

  listMessages(id: string, limit = 100) {
    return this.get<{ items: Message[] }>(`/api/sessions/${id}/messages`, { limit });
  }
  postMessage(id: string, content: Json, routing = "queue") {
    return this.post<{ message_id: string; turn_id?: string }>(
      `/api/sessions/${id}/messages`,
      { routing, content },
    );
  }
  listTurns(id: string) {
    return this.get<{ items: Turn[] }>(`/api/sessions/${id}/turns`);
  }
  cancelTurn(turnId: string) {
    return this.post(`/api/turns/${turnId}/cancellations`, {}, false);
  }
  listEvents(id: string, afterSeq = 0, limit = 500) {
    return this.get<{ items: SessionEvent[]; event_watermark: number }>(
      `/api/sessions/${id}/events`,
      { after_seq: afterSeq, limit },
    );
  }
  executor(id: string) {
    return this.get<{ active_lease: ExecutorLease | null; leases: ExecutorLease[] }>(
      `/api/sessions/${id}/executor`,
    );
  }
  listFiles(id: string, path = ".") {
    return this.get<{ entries: { path: string; kind: string }[] }>(
      `/api/sessions/${id}/files`,
      { path },
    );
  }
  readFile(id: string, path: string) {
    return this.get<{ content_b64: string }>(`/api/sessions/${id}/files/content`, {
      path,
    });
  }
  observeChanges(id: string) {
    return this.get<{ entries: unknown[] }>(`/api/sessions/${id}/changes`);
  }
  capture(id: string, body: Json = {}) {
    return this.post<{ changeset_id?: string; job_id?: string }>(
      `/api/sessions/${id}/changes`,
      body,
    );
  }
  listChangeSets(id: string) {
    return this.get<{ items: ChangeSet[] }>(`/api/sessions/${id}/changesets`);
  }
  getChangeSet(id: string) {
    return this.get<ChangeSet>(`/api/changesets/${id}`);
  }
  changeSetFiles(id: string) {
    return this.get<{ items: { path: string; content_digest: string }[] }>(
      `/api/changesets/${id}/files`,
    );
  }
  applyChangeSet(id: string, sessionId: string) {
    return this.post<{ job_id: string }>(`/api/changesets/${id}/applications`, {
      session_id: sessionId,
    });
  }
  createDelivery(
    changesetId: string,
    body: { target: Json; transport: string; connection_id?: string; ship_policy?: Json },
  ) {
    return this.post<{ delivery: Delivery }>(
      `/api/changesets/${changesetId}/deliveries`,
      body,
    );
  }
  getDelivery(id: string) {
    return this.get<Delivery>(`/api/deliveries/${id}`);
  }
  retryDelivery(id: string) {
    return this.post<{ job: Job }>(`/api/deliveries/${id}/retries`, {});
  }
  requestMerge(id: string, expectedHeadSha: string, mergeMethod = "squash") {
    return this.post<{ merge_request: MergeRequest }>(
      `/api/deliveries/${id}/merge-requests`,
      { expected_head_sha: expectedHeadSha, merge_method: mergeMethod },
    );
  }
  listDelegations(sessionId: string) {
    return this.get<{ items: Delegation[] }>(`/api/sessions/${sessionId}/delegations`);
  }
  spawnDelegation(
    sessionId: string,
    body: { role: string; prompt: string; result_contract?: Json; inputs?: Json[] },
  ) {
    return this.post<{ delegation: Delegation }>(
      `/api/sessions/${sessionId}/delegations`,
      body,
    );
  }
  getDelegation(id: string) {
    return this.get<Delegation & { result: DelegationResult | null }>(
      `/api/delegations/${id}`,
    );
  }
  cancelDelegation(id: string) {
    return this.post(`/api/delegations/${id}/cancellations`, {}, false);
  }
  getJob(id: string) {
    return this.get<Job>(`/api/jobs/${id}`);
  }
}

export const api = new UnifiedApi();

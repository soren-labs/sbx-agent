import { errorFromResponse, networkError } from "./errors";
import { Http, type HttpOptions, type RequestOptions } from "./http";
import { SseParser, parseEnvelope } from "./sse";
import type {
  Accepted,
  ApiKey,
  ChangeSet,
  Connection,
  ConnectionCredential,
  ConnectionKind,
  CreateSessionBody,
  CreateSessionResult,
  CreatedApiKey,
  Delegation,
  DelegationRole,
  Delivery,
  EventEnvelope,
  EventPage,
  ExecutorBackend,
  ExecutorView,
  FileContent,
  FileEntry,
  Harness,
  LiveChanges,
  LoginResult,
  Me,
  MergeRequestBody,
  MessageList,
  ModelsView,
  Project,
  ProjectSpec,
  ProjectVersion,
  ServiceItem,
  Session,
  SessionList,
  SessionSnapshot,
  TerminalOutput,
  Turn,
} from "./types";

/** Optional per-call settings for mutations. */
export interface Opts {
  idempotencyKey?: string;
  signal?: AbortSignal;
}

export interface SessionFilters {
  lifecycle?: string;
  role?: string;
  parent_session_id?: string;
  project_id?: string;
  cursor?: string | null;
  limit?: number;
}

export function createApiClient(options: HttpOptions = {}) {
  const http = new Http(options);
  const get = <T>(path: string, query?: RequestOptions["query"], signal?: AbortSignal) =>
    http.request<T>("GET", path, { query, signal });
  const send = <T>(method: string, path: string, body?: unknown, o?: Opts) =>
    http.request<T>(method, path, { body, idempotencyKey: o?.idempotencyKey, signal: o?.signal });
  const post = <T = Accepted>(path: string, body?: unknown, o?: Opts) => send<T>("POST", path, body, o);

  const fetchImpl = options.fetch ?? ((...a: Parameters<typeof fetch>) => globalThis.fetch(...a));

  return {
    http,
    setCsrf: (t: string | null) => http.setCsrf(t),
    setUnauthenticatedHandler: (fn: (() => void) | undefined) => {
      http.onUnauthenticated = fn;
    },

    auth: {
      register: (email: string, password: string, o?: Opts) =>
        post<{ status?: string }>("/api/auth/register", { email, password }, o),
      verifyEmail: (token: string, o?: Opts) =>
        post<{ status: string; email?: string }>("/api/auth/email-verifications", { token }, o),
      resendVerification: (email: string, o?: Opts) =>
        post<{ status: string }>("/api/auth/email-verifications", { email }, o),
      async login(email: string, password: string, o?: Opts) {
        const r = await post<LoginResult>("/api/auth/login", { email, password }, o);
        http.setCsrf(r.csrf_token);
        return r;
      },
      async logout(o?: Opts) {
        try {
          await post<void>("/api/auth/logout", undefined, o);
        } finally {
          http.setCsrf(null);
        }
      },
      changePassword: (current_password: string, new_password: string, o?: Opts) =>
        post<unknown>("/api/auth/password-changes", { current_password, new_password }, o),
      me: (signal?: AbortSignal) => get<Me>("/api/me", undefined, signal),
    },

    apiKeys: {
      list: () => get<{ items: ApiKey[] }>("/api/api-keys"),
      create: (name: string, o?: Opts) => post<CreatedApiKey>("/api/api-keys", { name }, o),
      revoke: (id: string, o?: Opts) => send<ApiKey>("DELETE", `/api/api-keys/${enc(id)}`, undefined, o),
    },

    connections: {
      list: (w: string) => get<{ items: Connection[] }>(`/api/workspaces/${enc(w)}/connections`),
      create: (
        w: string,
        body: { kind: ConnectionKind; label: string; credential: ConnectionCredential },
        o?: Opts,
      ) => post<Connection>(`/api/workspaces/${enc(w)}/connections`, body, o),
      get: (c: string) => get<Connection>(`/api/connections/${enc(c)}`),
      update: (c: string, body: { label: string; expected_version: number }, o?: Opts) =>
        send<Connection>("PATCH", `/api/connections/${enc(c)}`, body, o),
      replaceCredential: (
        c: string,
        body: { credential: ConnectionCredential; expected_version: number },
        o?: Opts,
      ) => post<Connection>(`/api/connections/${enc(c)}/credential-versions`, body, o),
      validate: (c: string, o?: Opts) => post<Accepted>(`/api/connections/${enc(c)}/validations`, undefined, o),
      disconnect: (c: string, o?: Opts) => send<Connection>("DELETE", `/api/connections/${enc(c)}`, undefined, o),
    },

    catalog: {
      harnesses: () => get<{ items: Harness[] }>("/api/harnesses"),
      models: (workspace_id: string, provider_id = "opencode") =>
        get<ModelsView>("/api/models", { workspace_id, provider_id }),
      executorBackends: () => get<{ items: ExecutorBackend[] }>("/api/executor-backends"),
    },

    projects: {
      list: (w: string) => get<{ items: Project[] }>(`/api/workspaces/${enc(w)}/projects`),
      create: (w: string, body: { slug: string; name: string; spec: ProjectSpec }, o?: Opts) =>
        post<Project>(`/api/workspaces/${enc(w)}/projects`, body, o),
      get: (p: string) => get<Project>(`/api/projects/${enc(p)}`),
      versions: (p: string) => get<{ items: ProjectVersion[] }>(`/api/projects/${enc(p)}/versions`),
      publishVersion: (p: string, body: { spec: ProjectSpec; expected_version: number }, o?: Opts) =>
        post<ProjectVersion>(`/api/projects/${enc(p)}/versions`, body, o),
    },

    sessions: {
      list: (w: string, f: SessionFilters = {}) =>
        get<SessionList>(`/api/workspaces/${enc(w)}/sessions`, { ...f }),
      create: (w: string, body: CreateSessionBody, o?: Opts) =>
        post<CreateSessionResult>(`/api/workspaces/${enc(w)}/sessions`, body, o),
      get: (s: string, signal?: AbortSignal) => get<SessionSnapshot>(`/api/sessions/${enc(s)}`, undefined, signal),
      update: (s: string, body: { title: string; expected_version: number }, o?: Opts) =>
        send<Session>("PATCH", `/api/sessions/${enc(s)}`, body, o),
      archive: (s: string, o?: Opts) => post<Session>(`/api/sessions/${enc(s)}/archives`, undefined, o),
      unarchive: (s: string, o?: Opts) => post<Session>(`/api/sessions/${enc(s)}/unarchives`, undefined, o),
      close: (s: string, o?: Opts) => post<Session>(`/api/sessions/${enc(s)}/closures`, undefined, o),
      messages: (s: string, signal?: AbortSignal) =>
        get<MessageList>(`/api/sessions/${enc(s)}/messages`, undefined, signal),
      sendMessage: (s: string, body: { content: string; routing?: "queue" | "note" }, o?: Opts) =>
        post<Accepted>(`/api/sessions/${enc(s)}/messages`, body, o),
      turns: (s: string) => get<{ items: Turn[] }>(`/api/sessions/${enc(s)}/turns`),
      eventsPage: (s: string, after: number, limit = 500, signal?: AbortSignal) =>
        get<EventPage>(`/api/sessions/${enc(s)}/events`, { after, limit }, signal),
      executor: (s: string) => get<ExecutorView>(`/api/sessions/${enc(s)}/executor`),
      activateExecutor: (s: string, o?: Opts) => post<Accepted>(`/api/sessions/${enc(s)}/executor/activations`, undefined, o),
      releaseExecutor: (s: string, o?: Opts) => post<Accepted>(`/api/sessions/${enc(s)}/executor/releases`, undefined, o),
      /**
       * SSE replay+tail after `after`. Resolves when the stream ends; rejects with
       * ApiError for non-2xx (e.g. invalid_cursor). Never creates server state.
       */
      async streamEvents(
        s: string,
        o: {
          after: number;
          signal?: AbortSignal;
          onEvent: (e: EventEnvelope) => void;
          /** Any bytes arrived (events or server heartbeats): the connection is alive. */
          onAlive?: () => void;
        },
      ): Promise<void> {
        let res: Response;
        try {
          res = await fetchImpl(http.url(`/api/sessions/${enc(s)}/events`, { after: o.after }), {
            method: "GET",
            credentials: "same-origin",
            headers: { Accept: "text/event-stream" },
            signal: o.signal,
          });
        } catch (e) {
          if (o.signal?.aborted) return;
          throw networkError(e);
        }
        if (!res.ok) {
          const body = await res.json().catch(() => null);
          throw errorFromResponse(res.status, body);
        }
        o.onAlive?.();
        if (!res.body) return;
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        const parser = new SseParser();
        try {
          for (;;) {
            const { value, done } = await reader.read();
            if (done) return;
            o.onAlive?.();
            for (const raw of parser.push(decoder.decode(value, { stream: true }))) {
              const env = parseEnvelope(raw);
              if (env) o.onEvent(env);
            }
          }
        } catch (e) {
          if (o.signal?.aborted) return;
          throw networkError(e);
        }
      },
    },

    turns: {
      cancel: (t: string, o?: Opts) => post<Accepted>(`/api/turns/${enc(t)}/cancellations`, undefined, o),
      retry: (t: string, o?: Opts) => post<Accepted>(`/api/turns/${enc(t)}/retries`, undefined, o),
      acknowledge: (t: string, o?: Opts) => post<Accepted>(`/api/turns/${enc(t)}/acknowledgements`, undefined, o),
    },

    changes: {
      live: (s: string) => get<LiveChanges>(`/api/sessions/${enc(s)}/changes`),
      list: (s: string) => get<{ items: ChangeSet[] }>(`/api/sessions/${enc(s)}/changesets`),
      capture: (s: string, body: { origin: "explicit" | "salvage"; source_turn_id?: string }, o?: Opts) =>
        post<{ changeset: ChangeSet; event_watermark: number }>(`/api/sessions/${enc(s)}/changesets`, body, o),
      get: (cs: string) => get<ChangeSet>(`/api/changesets/${enc(cs)}`),
      diff: (cs: string) =>
        get<{ diff: string; truncated?: boolean }>(`/api/changesets/${enc(cs)}/diff`),
      file: (cs: string, path: string) => get<FileContent>(`/api/changesets/${enc(cs)}/files`, { path }),
    },

    deliveries: {
      request: (cs: string, body: { title?: string; draft?: boolean }, o?: Opts) =>
        post<Accepted>(`/api/changesets/${enc(cs)}/deliveries`, body, o),
      list: (cs: string) => get<{ items: Delivery[] }>(`/api/changesets/${enc(cs)}/deliveries`),
      get: (d: string) => get<Delivery>(`/api/deliveries/${enc(d)}`),
      retry: (d: string, o?: Opts) => post<Accepted>(`/api/deliveries/${enc(d)}/retries`, undefined, o),
      refresh: (d: string, o?: Opts) => post<Accepted>(`/api/deliveries/${enc(d)}/refreshes`, undefined, o),
      requestMerge: (d: string, body: MergeRequestBody, o?: Opts) =>
        post<Accepted>(`/api/deliveries/${enc(d)}/merge-requests`, body, o),
    },

    delegations: {
      list: (s: string) => get<{ items: Delegation[] }>(`/api/sessions/${enc(s)}/delegations`),
      spawn: (
        s: string,
        body: { role: DelegationRole; changeset_id?: string; context?: string; instructions?: string },
        o?: Opts,
      ) => post<Accepted>(`/api/sessions/${enc(s)}/delegations`, body, o),
      get: (g: string) => get<Delegation>(`/api/delegations/${enc(g)}`),
      wait: (g: string, o?: Opts) => post<Accepted>(`/api/delegations/${enc(g)}/waits`, { wake: "none" }, o),
      cancel: (g: string, o?: Opts) => post<Accepted>(`/api/delegations/${enc(g)}/cancellations`, undefined, o),
      message: (g: string, body: { content: string }, o?: Opts) =>
        post<Accepted>(`/api/delegations/${enc(g)}/messages`, body, o),
    },

    files: {
      list: (s: string, path = "") => get<{ items: FileEntry[] }>(`/api/sessions/${enc(s)}/files`, { path }),
      read: (s: string, path: string) => get<FileContent>(`/api/sessions/${enc(s)}/files/content`, { path }),
      write: (s: string, body: { path: string; content: string; expected_digest: string }, o?: Opts) =>
        send<unknown>("PUT", `/api/sessions/${enc(s)}/files`, body, o),
    },

    terminals: {
      create: (s: string, o?: Opts) => post<{ terminal_id: string }>(`/api/sessions/${enc(s)}/terminals`, undefined, o),
      input: (s: string, t: string, data: string, o?: Opts) =>
        post<unknown>(`/api/sessions/${enc(s)}/terminals/${enc(t)}/input`, { data }, o),
      output: (s: string, t: string, after: number) =>
        get<TerminalOutput>(`/api/sessions/${enc(s)}/terminals/${enc(t)}/output`, { after }),
    },

    services: {
      list: (s: string) => get<{ items: ServiceItem[] }>(`/api/sessions/${enc(s)}/services`),
      activate: (s: string, name: string, o?: Opts) =>
        post<Accepted>(`/api/sessions/${enc(s)}/services/${enc(name)}/activations`, undefined, o),
      stop: (s: string, name: string, o?: Opts) =>
        post<Accepted>(`/api/sessions/${enc(s)}/services/${enc(name)}/stops`, undefined, o),
      logs: (s: string, name: string) =>
        get<{ lines: string[] }>(`/api/sessions/${enc(s)}/services/${enc(name)}/logs`),
    },
  };
}

const enc = encodeURIComponent;

export type ApiClient = ReturnType<typeof createApiClient>;

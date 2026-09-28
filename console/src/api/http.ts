import { ApiError } from "./client";
import type { SessionApi, SessionEventHandlers } from "./client";
import {
  endReasonOf,
  normalizeEvent,
  normalizePhase,
  normalizeTurnStatus,
  toApiError,
  toErrorKind,
  toTurnError,
} from "./normalize";
import type {
  IntegrationStatus,
  NewSessionInput,
  ProviderId,
  ProviderInfo,
  Session,
  SessionChange,
  Turn,
  Usage,
} from "./types";

/**
 * HTTP SessionApi over the frozen public contract.
 *
 * Route table is centralized here so the SOR-262 integration can retarget
 * the client (e.g. to session-named V2 routes) without touching UI code.
 * Today it maps the contract's agent≙session / run≙turn resources.
 */
const PATHS = {
  sessions: "/v1/agents",
  session: (id: string) => `/v1/agents/${id}`,
  runs: (id: string) => `/v1/agents/${id}/runs`,
  runStream: (id: string, runId: string) =>
    `/v1/agents/${id}/runs/${runId}/stream`,
  cancelRun: (id: string, runId: string) =>
    `/v1/agents/${id}/runs/${runId}/cancel`,
  revisions: (id: string) => `/v1/agents/${id}/revisions`,
  usage: (id: string) => `/v1/agents/${id}/usage`,
  providers: "/v1/providers",
  models: "/v1/models",
  githubApp: "/v1/github/app",
  authConnect: "/v1/auth/connect",
} as const;

const TOKEN_KEY = "sbx.console.token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}
export function setToken(token: string) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function baseUrl(): string {
  return (import.meta.env.VITE_API_BASE as string | undefined) ?? "";
}

interface RawErrorBody {
  error?:
    | string
    | {
        code?: string;
        message?: string;
        retryable?: boolean;
        retry_after?: number;
      };
  code?: number;
  message?: string;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
  };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(`${baseUrl()}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw toApiError(e);
  }
  if (!res.ok) {
    let parsed: RawErrorBody = {};
    try {
      parsed = (await res.json()) as RawErrorBody;
    } catch {
      /* non-JSON error body */
    }
    const errField = parsed.error;
    const subcode =
      typeof errField === "string"
        ? errField
        : typeof errField === "object" && errField
          ? (errField.code ?? "internal")
          : "internal";
    const message =
      typeof errField === "object" && errField?.message
        ? errField.message
        : (parsed.message ?? `Request failed (${res.status})`);
    throw new ApiError(toErrorKind(subcode, res.status), message, {
      httpStatus: res.status,
      subcode,
      retryable:
        typeof errField === "object" ? Boolean(errField?.retryable) : false,
      retryAfter:
        typeof errField === "object" && typeof errField.retry_after === "number"
          ? errField.retry_after
          : undefined,
    });
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/* ---------- backend → product mapping ---------- */

type Raw = Record<string, unknown>;
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" ? v : null);

function mapUsage(raw: unknown): Usage | null {
  if (!raw || typeof raw !== "object") return null;
  const u = raw as Raw;
  return {
    inputTokens: num(u.input_tokens) ?? 0,
    cachedInputTokens: num(u.cached_input_tokens) ?? 0,
    outputTokens: num(u.output_tokens) ?? 0,
    cacheWriteInputTokens: num(u.cache_write_input_tokens) ?? undefined,
    reasoningOutputTokens: num(u.reasoning_output_tokens) ?? undefined,
  };
}

function mapRepo(raw: unknown): Session["repo"] {
  if (!raw || typeof raw !== "object") return null;
  const w = raw as Raw;
  const repo = str(w.repo) ?? str(w.repository) ?? str(w.url);
  if (!repo) return null;
  const name = repo.replace(/^https:\/\/github\.com\//, "").replace(/\.git$/, "");
  return { name, url: `https://github.com/${name}`, ref: str(w.ref) ?? str(w.base_ref) ?? undefined };
}

function mapRun(raw: Raw, index: number): Turn {
  const prompt = raw.prompt as Raw | undefined;
  const result = raw.result as Raw | undefined;
  return {
    id: str(raw.id) ?? `run-${index}`,
    index,
    prompt: str(prompt?.text) ?? "",
    status: normalizeTurnStatus(str(raw.status)),
    createdAt: str(raw.created_at) ?? new Date().toISOString(),
    startedAt: str(raw.started_at),
    finishedAt: str(raw.finished_at),
    result: str(result?.text),
    error: toTurnError(raw.error),
    activity: [],
  };
}

function mapSession(raw: Raw, runs: Turn[] = []): Session {
  const status = str(raw.status);
  const compute = raw.compute as Raw | undefined;
  const error = toTurnError(raw.error);
  const lastTurn = runs[runs.length - 1];
  const phase = normalizePhase(status);
  return {
    id: str(raw.id) ?? "",
    title:
      str(raw.name) ??
      str(raw.title) ??
      (lastTurn?.prompt.split("\n")[0].slice(0, 80) || "Session"),
    phase: phase === "ended" && error ? "failed" : phase,
    endReason: endReasonOf(status),
    provider: (str(raw.provider) ?? "codex") as ProviderId,
    model: str(raw.model) ?? "auto",
    accountLabel: str(raw.account_label) ?? null,
    repo: mapRepo(raw.workspace ?? raw.repo),
    effort: (str(raw.reasoning_effort) as Session["effort"]) ?? null,
    compute:
      compute && Array.isArray(compute.cpu) && Array.isArray(compute.memory_mib)
        ? {
            cpu: [Number(compute.cpu[0]), Number(compute.cpu[1])],
            memoryMib: [
              Number(compute.memory_mib[0]),
              Number(compute.memory_mib[1]),
            ],
          }
        : null,
    idleTimeoutS: num(raw.idle_timeout_s),
    delivery: null,
    createdAt: str(raw.created_at) ?? new Date().toISOString(),
    updatedAt: str(raw.updated_at) ?? new Date().toISOString(),
    usage: mapUsage(raw.usage),
    costUsd: num(raw.cost_estimate_usd),
    runtimeSeconds: num(raw.sandbox_seconds),
    turns: runs,
    lastActivityPreview: null,
    hasChanges: false,
    error,
  };
}

/* ---------- SSE over fetch (supports Authorization + Last-Event-ID) ---------- */

function parseSseBlock(block: string): { id?: string; event?: string; data?: string } | null {
  let id: string | undefined;
  let event: string | undefined;
  const dataLines: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) return null; // keepalive / comment
    if (line.startsWith("id:")) id = line.slice(3).trim();
    else if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  return { id, event, data: dataLines.length ? dataLines.join("\n") : undefined };
}

/* ---------- client ---------- */

export class HttpSessionApi implements SessionApi {
  private async runsFor(sessionId: string): Promise<Turn[]> {
    try {
      const raw = await request<unknown>("GET", PATHS.runs(sessionId));
      const list = Array.isArray(raw)
        ? raw
        : ((raw as Raw)?.runs as unknown[]) ?? [];
      return (list as Raw[]).map((r, i) => mapRun(r, i + 1));
    } catch {
      return [];
    }
  }

  async listSessions(): Promise<Session[]> {
    const raw = await request<unknown>("GET", PATHS.sessions);
    const list = Array.isArray(raw)
      ? raw
      : ((raw as Raw)?.agents as unknown[]) ?? [];
    return (list as Raw[]).map((a) => mapSession(a));
  }

  async getSession(id: string): Promise<Session> {
    const raw = (await request<Raw>("GET", PATHS.session(id))) as Raw;
    const runs = await this.runsFor(id);
    return mapSession(raw, runs);
  }

  async createSession(input: NewSessionInput): Promise<Session> {
    const body: Record<string, unknown> = {
      prompt: { text: input.prompt },
      agent: {
        provider: input.provider && input.provider !== "auto" ? input.provider : "codex",
        account_id: input.account ?? "auto",
        model: input.model && input.model !== "auto" ? input.model : undefined,
        reasoning_effort: input.effort,
      },
      name: input.title,
      idle_timeout_s: input.idleTimeoutS,
    };
    if (input.repo) {
      body.workspace = { repo: input.repo };
    }
    if (input.compute?.cpu || input.compute?.memoryMib) {
      body.compute = {
        cpu: input.compute.cpu,
        memory_mib: input.compute.memoryMib,
      };
    }
    const raw = (await request<Raw>(
      "POST",
      PATHS.sessions,
      body,
    )) as Raw;
    const id = str(raw.id) ?? str(raw.agent_id) ?? "";
    // Optimistic shell; the caller navigates immediately and subscribes.
    const session = mapSession(raw, [
      {
        id: str(raw.run_id) ?? `run-${id}-1`,
        index: 1,
        prompt: input.prompt,
        status: "queued",
        createdAt: new Date().toISOString(),
        startedAt: null,
        finishedAt: null,
        result: null,
        error: null,
        activity: [],
      },
    ]);
    if (!session.id) session.id = id;
    session.phase = "queued";
    return session;
  }

  async sendFollowUp(
    sessionId: string,
    text: string,
  ): Promise<{ turnId: string }> {
    const raw = (await request<Raw>("POST", PATHS.runs(sessionId), {
      prompt: { text },
    })) as Raw;
    return { turnId: str(raw.id) ?? str(raw.run_id) ?? "" };
  }

  async stopSession(sessionId: string): Promise<void> {
    const runs = await this.runsFor(sessionId);
    const active = [...runs].reverse().find(
      (t) => t.status === "running" || t.status === "queued",
    );
    if (!active) return;
    await request("POST", PATHS.cancelRun(sessionId, active.id));
  }

  async closeSession(sessionId: string): Promise<Session> {
    const raw = (await request<Raw>(
      "DELETE",
      PATHS.session(sessionId),
    )) as Raw;
    return mapSession(raw);
  }

  async listProviders(): Promise<ProviderInfo[]> {
    const raw = await request<unknown>("GET", PATHS.providers);
    const list = Array.isArray(raw) ? raw : ((raw as Raw)?.providers as unknown[]) ?? [];
    return (list as Raw[]).map((p) => {
      const status = str(p.status);
      const models = Array.isArray(p.models)
        ? (p.models as unknown[]).map((m) =>
            typeof m === "string" ? m : str((m as Raw).id) ?? "",
          )
        : [];
      return {
        id: (str(p.provider) ?? str(p.id) ?? "codex") as ProviderId,
        label: str(p.label) ?? str(p.provider) ?? "Provider",
        models,
        efforts: [],
        accountsTotal: num(p.accounts_total) ?? 0,
        accountsAvailable: num(p.accounts_available) ?? 0,
        needsLogin: status === "needs_login" || status === "unauthenticated",
      };
    });
  }

  async getIntegrations(): Promise<IntegrationStatus> {
    const [providers, github] = await Promise.all([
      this.listProviders(),
      request<Raw>("GET", PATHS.githubApp).catch(() => null),
    ]);
    return {
      providers,
      github: {
        configured: Boolean(github),
        connected: Boolean(github && (github as Raw).installation),
        account: str((github as Raw | null)?.account) ?? undefined,
        installUrl: str((github as Raw | null)?.install_url) ?? undefined,
      },
      runtime: { enabled: true },
    };
  }

  async listChanges(sessionId: string): Promise<SessionChange[]> {
    const raw = await request<unknown>("GET", PATHS.revisions(sessionId)).catch(
      () => [],
    );
    const list = Array.isArray(raw)
      ? raw
      : ((raw as Raw)?.revisions as unknown[]) ?? [];
    return (list as Raw[]).map((r, i) => ({
      id: str(r.id) ?? `rev-${i}`,
      kind: "revision",
      summary: str(r.summary) ?? str(r.ref) ?? `Revision ${i + 1}`,
      ts: str(r.created_at) ?? "",
      ref: str(r.ref) ?? undefined,
      url: str(r.pr_url) ?? str(r.url) ?? undefined,
    }));
  }

  subscribe(sessionId: string, handlers: SessionEventHandlers): () => void {
    const abort = new AbortController();
    let lastEventId: string | null = null;
    let retry = 0;

    const connect = async () => {
      while (!abort.signal.aborted) {
        try {
          const runs = await this.runsFor(sessionId);
          const active = [...runs].reverse().find(
            (t) => t.status === "running" || t.status === "queued",
          ) ?? runs[runs.length - 1];
          if (!active) return;
          const headers: Record<string, string> = {
            Accept: "text/event-stream",
          };
          const token = getToken();
          if (token) headers.Authorization = `Bearer ${token}`;
          if (lastEventId) headers["Last-Event-ID"] = lastEventId;
          const res = await fetch(
            `${baseUrl()}${PATHS.runStream(sessionId, active.id)}`,
            { headers, signal: abort.signal },
          );
          if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
          retry = 0;
          handlers.onReconnect?.();
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let buf = "";
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            let idx: number;
            while ((idx = buf.indexOf("\n\n")) >= 0) {
              const block = buf.slice(0, idx);
              buf = buf.slice(idx + 2);
              const frame = parseSseBlock(block);
              if (!frame || frame.data === undefined) continue;
              if (frame.id) lastEventId = frame.id;
              let parsed: unknown;
              try {
                parsed = JSON.parse(frame.data);
              } catch {
                continue;
              }
              const item = normalizeEvent(parsed, frame.id);
              if (item) handlers.onActivity?.(item);
            }
          }
          throw new Error("stream ended");
        } catch (e) {
          if (abort.signal.aborted) return;
          retry += 1;
          const delay = Math.min(30_000, 500 * 2 ** retry);
          handlers.onDisconnect?.(delay);
          await new Promise((r) => setTimeout(r, delay));
        }
      }
    };
    void connect();
    return () => abort.abort();
  }
}

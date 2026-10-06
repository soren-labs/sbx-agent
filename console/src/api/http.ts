import { ApiError, errorFromResponse, networkError } from "./errors";

export interface HttpOptions {
  fetch?: typeof fetch;
  baseUrl?: string;
  /** Extra attempts for mutations after a network failure / 502-504 (same Idempotency-Key). */
  maxRetries?: number;
  retryDelayMs?: number;
  newKey?: () => string;
  onUnauthenticated?: () => void;
}

export interface RequestOptions {
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
  /** Provide to retry a previously uncertain mutation with the same key. */
  idempotencyKey?: string;
  signal?: AbortSignal;
}

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const CSRF_COOKIE = "sbx_csrf";

export function randomKey(): string {
  const c = globalThis.crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `k_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`;
}

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  for (const part of document.cookie.split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return decodeURIComponent(v.join("="));
  }
  return null;
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

export class Http {
  private csrf: string | null = null;
  private readonly fetchImpl: typeof fetch;
  readonly baseUrl: string;
  private readonly maxRetries: number;
  private readonly retryDelayMs: number;
  private readonly newKey: () => string;
  onUnauthenticated?: () => void;

  constructor(opts: HttpOptions = {}) {
    this.fetchImpl = opts.fetch ?? ((...a) => globalThis.fetch(...a));
    this.baseUrl = opts.baseUrl ?? "";
    this.maxRetries = opts.maxRetries ?? 2;
    this.retryDelayMs = opts.retryDelayMs ?? 250;
    this.newKey = opts.newKey ?? randomKey;
    this.onUnauthenticated = opts.onUnauthenticated;
  }

  setCsrf(token: string | null) {
    this.csrf = token;
  }

  csrfToken(): string | null {
    return this.csrf ?? readCookie(CSRF_COOKIE);
  }

  url(path: string, query?: RequestOptions["query"]): string {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query ?? {})) {
      if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    }
    const s = qs.toString();
    return `${this.baseUrl}${path}${s ? `?${s}` : ""}`;
  }

  headers(method: string, extra: Record<string, string> = {}): Record<string, string> {
    const h: Record<string, string> = { Accept: "application/json", ...extra };
    if (MUTATING.has(method)) {
      const csrf = this.csrfToken();
      if (csrf) h["X-CSRF-Token"] = csrf;
    }
    return h;
  }

  async request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const mutating = MUTATING.has(method);
    const key = mutating ? (opts.idempotencyKey ?? this.newKey()) : null;
    const extra: Record<string, string> = {};
    if (opts.body !== undefined) extra["Content-Type"] = "application/json";
    if (key) extra["Idempotency-Key"] = key;
    const init: RequestInit = {
      method,
      credentials: "same-origin",
      headers: this.headers(method, extra),
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: opts.signal,
    };
    const url = this.url(path, opts.query);
    const attempts = mutating ? this.maxRetries + 1 : 1;
    let last: ApiError | null = null;
    for (let attempt = 0; attempt < attempts; attempt++) {
      if (attempt > 0) await sleep(this.retryDelayMs * attempt);
      let res: Response;
      try {
        res = await this.fetchImpl(url, init);
      } catch (cause) {
        if (opts.signal?.aborted) throw cause;
        last = networkError(cause);
        continue;
      }
      if (res.ok) return (await parseBody(res)) as T;
      const err = errorFromResponse(res.status, await parseBody(res).catch(() => null));
      if (res.status === 401 && err.code === "unauthenticated" && !path.startsWith("/api/auth/login")) {
        this.onUnauthenticated?.();
      }
      if (mutating && [502, 503, 504].includes(res.status)) {
        last = err;
        continue;
      }
      err.idempotencyKey = key;
      throw err;
    }
    const outcomeUnknown = new ApiError({
      status: last?.status ?? 0,
      code: last && last.status !== 0 ? last.code : "outcome_unknown",
      category: "transport",
      message: "The request may or may not have been applied. Retry to reuse the same request key.",
      retryable: true,
      details: last?.details,
      requestId: last?.requestId,
    });
    outcomeUnknown.idempotencyKey = key;
    throw outcomeUnknown;
  }
}

async function parseBody(res: Response): Promise<unknown> {
  if (res.status === 204) return undefined;
  const text = await res.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

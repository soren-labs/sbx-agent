import type { ErrorBody } from "./types";

/** Canonical API error; `status === 0` means the request never got a response. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly category: string;
  readonly retryable: boolean;
  readonly details: Record<string, unknown>;
  readonly requestId: string | null;
  readonly action: string | null;
  /** Key used by a mutation whose outcome is unknown; reuse it to retry safely. */
  idempotencyKey: string | null = null;

  constructor(init: {
    status: number;
    code: string;
    category?: string;
    message?: string;
    retryable?: boolean;
    details?: Record<string, unknown> | null;
    requestId?: string | null;
    action?: string | null;
  }) {
    super(init.message || init.code);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.category = init.category ?? "unknown";
    this.retryable = init.retryable ?? false;
    this.details = init.details ?? {};
    this.requestId = init.requestId ?? null;
    this.action = init.action ?? null;
  }

  get isNetwork() {
    return this.status === 0;
  }
}

export function errorFromResponse(status: number, body: unknown): ApiError {
  const e = (body as Partial<ErrorBody> | null)?.error;
  if (e && typeof e.code === "string") {
    return new ApiError({
      status,
      code: e.code,
      category: e.category,
      message: e.message,
      retryable: e.retryable,
      details: e.details,
      requestId: e.request_id,
      action: e.action,
    });
  }
  return new ApiError({
    status,
    code: `http_${status}`,
    category: "transport",
    message: `Request failed (${status})`,
    retryable: status >= 500,
  });
}

export function networkError(cause: unknown): ApiError {
  return new ApiError({
    status: 0,
    code: "network_error",
    category: "transport",
    message: cause instanceof Error ? cause.message : "Network error",
    retryable: true,
  });
}

export const isApiError = (e: unknown): e is ApiError => e instanceof ApiError;

/** Errors after which the cached snapshot must be refetched. */
export const isResyncError = (e: unknown) =>
  isApiError(e) && (e.code === "invalid_cursor" || e.code === "history_reset_required");

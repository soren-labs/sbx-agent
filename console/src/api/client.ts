import type {
  ErrorKind,
  IntegrationStatus,
  NewSessionInput,
  ProviderInfo,
  Session,
  SessionChange,
  SessionPhase,
  ActivityItem,
  Turn,
} from "./types";

/** API error carrying a product-level kind for actionable UX. */
export class ApiError extends Error {
  readonly kind: ErrorKind;
  readonly httpStatus: number;
  readonly subcode: string;
  readonly retryable: boolean;
  readonly retryAfter?: number;

  constructor(
    kind: ErrorKind,
    message: string,
    opts: {
      httpStatus?: number;
      subcode?: string;
      retryable?: boolean;
      retryAfter?: number;
    } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.httpStatus = opts.httpStatus ?? 0;
    this.subcode = opts.subcode ?? "internal";
    this.retryable = opts.retryable ?? false;
    this.retryAfter = opts.retryAfter;
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

/** Live event surface for one session (SSE or fixture emitter). */
export interface SessionEventHandlers {
  onPhase?: (phase: SessionPhase) => void;
  onActivity?: (item: ActivityItem) => void;
  onTurn?: (turn: Turn) => void;
  onSession?: (session: Session) => void;
  onError?: (error: ApiError) => void;
  /** Fired when a dropped stream is re-established. */
  onReconnect?: () => void;
  /** Fired when the stream is down and a retry is pending. */
  onDisconnect?: (nextRetryMs: number) => void;
}

export interface SessionApi {
  listSessions(): Promise<Session[]>;
  getSession(id: string): Promise<Session>;
  /**
   * Create a session. Resolves once the session shell exists (queued or
   * starting) — the UI navigates to it optimistically and follows live
   * events via subscribe().
   */
  createSession(input: NewSessionInput): Promise<Session>;
  /** Submit a follow-up turn. 202-style accept; completion arrives via events. */
  sendFollowUp(sessionId: string, text: string): Promise<{ turnId: string }>;
  stopSession(sessionId: string): Promise<void>;
  closeSession(sessionId: string): Promise<Session>;
  listProviders(): Promise<ProviderInfo[]>;
  getIntegrations(): Promise<IntegrationStatus>;
  listChanges(sessionId: string): Promise<SessionChange[]>;
  /** Subscribe to the session's live event stream. Returns an unsubscribe. */
  subscribe(sessionId: string, handlers: SessionEventHandlers): () => void;
}

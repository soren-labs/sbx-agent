import { useCallback, useRef, useState, type ReactNode } from "react";
import { randomKey } from "../api/http";
import { isApiError } from "../api/errors";
import { useI18n } from "../i18n";
import type { I18nKey } from "../i18n/en";
import { Icon, Spinner } from "./icons";

export type Tone = "ok" | "warn" | "err" | "run" | "dim";

export function Pill({ tone = "dim", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`pill tone-${tone}`} title={title}>
      <span className="dot" aria-hidden="true" />
      {children}
    </span>
  );
}

export const healthTone = (h: string): Tone =>
  h === "ready" ? "ok" : h === "degraded" || h === "verifying" || h === "unverified" ? "warn" : "err";

export function Loading({ label }: { label?: string }) {
  const { t } = useI18n();
  return (
    <div className="row muted small" role="status">
      <Spinner /> {label ?? t("common.loading")}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

const KNOWN: Partial<Record<string, I18nKey>> = {
  executor_unavailable: "error.executor_unavailable",
  connection_in_use: "error.connection_in_use",
  version_conflict: "error.version_conflict",
  outcome_unknown: "error.outcome_unknown",
  unauthenticated: "error.unauthenticated",
  credential_invalid: "error.credential_invalid",
  unsupported_capability: "error.unsupported_capability",
  idempotency_conflict: "error.idempotency_conflict",
};

/** Renders a canonical API error. `executor_unavailable` is a diagnosis, never auto-healed. */
export function ErrorNotice({
  error,
  onRetry,
  retryLabel,
}: {
  error: unknown;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  const { t } = useI18n();
  if (!error) return null;
  const api = isApiError(error) ? error : null;
  const known = api ? KNOWN[api.code] : undefined;
  const message = error instanceof Error ? error.message : String(error);
  const diagnostic = api && Object.keys(api.details).length ? JSON.stringify(api.details) : null;
  return (
    <div className="notice" role="alert">
      <Icon name="warn" />
      <div className="grow">
        <div className="n-title">{known ? t(known) : (api?.code ?? t("error.generic"))}</div>
        <div className="n-body">{message}</div>
        {diagnostic ? <div className="n-body mono small">{diagnostic}</div> : null}
        {api?.requestId ? (
          <div className="faint small">
            {t("error.request_id")}: <code>{api.requestId}</code>
          </div>
        ) : null}
        {onRetry ? (
          <div className="n-actions">
            <button type="button" className="btn btn-sm" onClick={onRetry}>
              {retryLabel ?? t("common.retry")}
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

/**
 * Runs a mutation with a stable Idempotency-Key: the key is reused only while the
 * previous attempt's outcome is unknown, and renewed after any definite result.
 */
export function useAction<A extends unknown[], R>(fn: (key: string, ...args: A) => Promise<R>) {
  const keyRef = useRef<string | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const run = useCallback(async (...args: A): Promise<R | undefined> => {
    const key = (keyRef.current ??= randomKey());
    setPending(true);
    setError(null);
    try {
      const r = await fnRef.current(key, ...args);
      keyRef.current = null;
      return r;
    } catch (e) {
      const unknown = isApiError(e) && (e.code === "outcome_unknown" || e.status === 0);
      if (!unknown) keyRef.current = null;
      setError(e);
      return undefined;
    } finally {
      setPending(false);
    }
  }, []);
  const reset = useCallback(() => {
    keyRef.current = null;
    setError(null);
  }, []);
  return { run, pending, error, reset };
}

export function Field({
  id,
  label,
  hint,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children}
      {hint ? <span className="hint">{hint}</span> : null}
    </div>
  );
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(-8) : "—";
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

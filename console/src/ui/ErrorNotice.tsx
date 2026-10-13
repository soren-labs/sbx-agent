import { AlertTriangle } from "lucide-react";
import { isApiError } from "../api/errors";
import { useI18n } from "../i18n";
import type { I18nKey } from "../i18n/en";
import { Button } from "./Button";

const KNOWN: Partial<Record<string, I18nKey>> = {
  executor_unavailable: "error.executor_unavailable",
  connection_in_use: "error.connection_in_use",
  version_conflict: "error.version_conflict",
  outcome_unknown: "error.outcome_unknown",
  unauthenticated: "error.unauthenticated",
  credential_invalid: "error.credential_invalid",
  unsupported_capability: "error.unsupported_capability",
  idempotency_conflict: "error.idempotency_conflict",
  connection_required: "error.connection_required",
  validation_failed: "error.validation_failed",
  rate_limited: "error.rate_limited",
  not_found: "error.not_found",
  forbidden: "error.forbidden",
};

/** Renders a canonical API error. `executor_unavailable` is a diagnosis, never auto-healed. */
export function ErrorNotice({
  error,
  onRetry,
  retryLabel,
  title,
}: {
  error: unknown;
  onRetry?: () => void;
  retryLabel?: string;
  /** Overrides the code-derived heading where the generic one would mislead. */
  title?: string;
}) {
  const { t } = useI18n();
  if (!error) return null;
  const api = isApiError(error) ? error : null;
  const known = api ? KNOWN[api.code] : undefined;
  const message = error instanceof Error ? error.message : String(error);
  const diagnostic = api && Object.keys(api.details).length ? JSON.stringify(api.details) : null;
  return (
    <div className="notice" role="alert">
      <AlertTriangle size={15} className="notice__icon" />
      <div className="notice__body">
        <div className="notice__title">{title ?? (known ? t(known) : (api?.code ?? t("error.generic")))}</div>
        <div className="notice__text">{message}</div>
        {diagnostic ? (
          <details className="notice__details">
            <summary>{t("error.details")}</summary>
            <code>{diagnostic}</code>
          </details>
        ) : null}
        {api?.requestId ? (
          <div className="faint small">
            {t("error.request_id")}: <code className="mono">{api.requestId}</code>
          </div>
        ) : null}
        {onRetry ? (
          <div className="notice__actions">
            <Button size="sm" variant="secondary" onClick={onRetry}>
              {retryLabel ?? t("common.retry")}
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

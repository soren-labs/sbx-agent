import { Link } from "react-router-dom";
import { isApiError, type ApiError } from "../api";
import type { ErrorKind } from "../api/types";
import { useI18n } from "../i18n";
import { Icon } from "./icons";

interface Props {
  error: ApiError | Error | string | null;
  provider?: string;
  onRetry?: () => void;
  onDismiss?: () => void;
  /** Extra escape route beside the primary action — e.g. a provider-sourced
   * failure also offers Integrations next to Retry. */
  secondary?: { label: string; to: string };
}

/** Product-level, actionable error surface. */
export function ErrorNotice({ error, provider, onRetry, onDismiss, secondary }: Props) {
  const { t } = useI18n();
  if (!error) return null;

  const apiErr = isApiError(error) ? error : null;
  const kind: ErrorKind = apiErr?.kind ?? "unknown";
  const detail = error instanceof Error ? error.message : error;
  const vars = { provider: provider ?? "provider", detail };

  let title = "";
  let body = "";
  let action: { label: string; to?: string; onClick?: () => void } | null = null;
  let warn = false;

  switch (kind) {
    case "provider_login":
      title = t("error.provider_login.title");
      body = t("error.provider_login.body", vars);
      action = { label: t("error.provider_login.action"), to: "/integrations" };
      break;
    case "provider_busy":
      title = t("error.provider_busy.title");
      body = t("error.provider_busy.body", vars);
      action = onRetry ? { label: t("error.provider_busy.action"), onClick: onRetry } : null;
      warn = true;
      break;
    case "runtime_disabled":
      title = t("error.runtime_disabled.title");
      body = t("error.runtime_disabled.body");
      // Runtime state lives on Integrations — never a bare 500.
      action = { label: t("error.runtime_disabled.action"), to: "/integrations" };
      break;
    case "github_required":
      title = t("error.github_required.title");
      body = t("error.github_required.body");
      action = { label: t("error.github_required.action"), to: "/integrations" };
      break;
    case "session_failed":
      title = t("error.session_failed.title");
      body = t("error.session_failed.body", {
        detail: detail ? `: ${detail}` : "",
      });
      action = onRetry ? { label: t("error.session_failed.action"), onClick: onRetry } : null;
      break;
    case "unauthorized":
      title = t("error.unauthorized.title");
      body = t("error.unauthorized.body");
      action = { label: t("error.unauthorized.action"), to: "/settings" };
      break;
    case "not_found":
      title = t("error.not_found.title");
      body = t("error.not_found.body");
      break;
    case "conflict":
      title = t("error.conflict.title");
      body = t("error.conflict.body");
      warn = true;
      break;
    case "network":
      title = t("error.network.title");
      body = t("error.network.body");
      action = onRetry ? { label: t("common.retry"), onClick: onRetry } : null;
      warn = true;
      break;
    default:
      title = t("error.generic.title");
      body = t("error.generic.body", { detail });
  }

  return (
    <div className={`notice${warn ? " warn" : ""}`} role="alert" data-testid="error-notice" data-kind={kind}>
      <Icon name="warn" size={18} />
      <div className="grow">
        <div className="n-title">{title}</div>
        <div className="n-body">{body}</div>
        {(action || secondary) && (
          <div className="n-actions">
            {action &&
              (action.to ? (
                <Link className="btn btn-sm" to={action.to}>{action.label}</Link>
              ) : (
                <button className="btn btn-sm" onClick={action.onClick}>{action.label}</button>
              ))}
            {secondary && (
              <Link className="btn btn-sm btn-ghost" to={secondary.to}>
                {secondary.label}
              </Link>
            )}
          </div>
        )}
      </div>
      {onDismiss && (
        <button className="btn btn-ghost btn-sm n-x" onClick={onDismiss} aria-label={t("error.dismiss")}>
          <Icon name="x" size={14} />
        </button>
      )}
    </div>
  );
}

/** Live-stream drop banner with auto-reconnect info. */
export function ReconnectBanner({
  retryInMs,
  onReconnect,
  reconnected,
}: {
  retryInMs: number | null;
  onReconnect?: () => void;
  reconnected?: boolean;
}) {
  const { t } = useI18n();
  if (reconnected) {
    return (
      <div className="phase-banner" role="status" data-testid="reconnected-banner">
        <Icon name="check" size={15} /> {t("error.reconnected")}
      </div>
    );
  }
  if (retryInMs == null) return null;
  return (
    <div className="notice warn" role="alert" data-testid="reconnect-banner">
      <Icon name="warn" size={18} />
      <div className="grow">
        <div className="n-title">{t("error.reconnect.title")}</div>
        <div className="n-body">
          {t("error.reconnect.body", { s: Math.ceil(retryInMs / 1000) })}
        </div>
      </div>
      {onReconnect && (
        <button className="btn btn-sm" onClick={onReconnect}>
          {t("error.reconnect.action")}
        </button>
      )}
    </div>
  );
}

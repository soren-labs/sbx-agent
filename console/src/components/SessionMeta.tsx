import type { Session, SessionChange } from "../api/types";
import { useI18n } from "../i18n";

function fmtDate(isoStr: string): string {
  const d = new Date(isoStr);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtDuration(s: number | null): string {
  if (s == null) return "—";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

function Kv({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </div>
  );
}

/** Compact Details / Usage / Runtime rail for the session page. */
export function SessionMeta({ session }: { session: Session }) {
  const { t } = useI18n();
  return (
    <div className="detail-rail" data-testid="session-meta">
      <div className="rail-section">
        <h2>{t("session.details")}</h2>
        <div className="card rail-card">
          <Kv k={t("detail.provider")} v={session.provider} />
          <Kv k={t("detail.model")} v={session.model} />
          {session.accountLabel && (
            <Kv k={t("detail.account")} v={session.accountLabel} />
          )}
          {session.repo && (
            <Kv
              k={t("detail.repo")}
              v={
                session.repo.url ? (
                  <a href={session.repo.url} target="_blank" rel="noreferrer">
                    {session.repo.name}
                  </a>
                ) : (
                  session.repo.name
                )
              }
            />
          )}
          <Kv k={t("detail.effort")} v={session.effort ?? t("detail.none")} />
          <Kv
            k={t("detail.delivery")}
            v={session.delivery?.mode ?? t("detail.none")}
          />
          <Kv
            k={t("detail.idle_timeout")}
            v={session.idleTimeoutS ? `${session.idleTimeoutS}s` : t("detail.none")}
          />
        </div>
      </div>
      <div className="rail-section">
        <h2>{t("session.usage")}</h2>
        <div className="card rail-card">
          <Kv
            k={t("detail.tokens_in")}
            v={session.usage ? session.usage.inputTokens.toLocaleString() : "—"}
          />
          <Kv
            k={t("detail.tokens_cached")}
            v={session.usage ? session.usage.cachedInputTokens.toLocaleString() : "—"}
          />
          <Kv
            k={t("detail.tokens_out")}
            v={session.usage ? session.usage.outputTokens.toLocaleString() : "—"}
          />
          <Kv
            k={t("detail.cost")}
            v={session.costUsd != null ? `$${session.costUsd.toFixed(3)}` : "—"}
          />
        </div>
      </div>
      <div className="rail-section">
        <h2>{t("session.runtime")}</h2>
        <div className="card rail-card">
          <Kv
            k={t("detail.compute")}
            v={
              session.compute
                ? `${session.compute.cpu[0]}–${session.compute.cpu[1]} CPU · ${session.compute.memoryMib[0]}–${session.compute.memoryMib[1]} MiB`
                : t("detail.none")
            }
          />
          <Kv
            k={t("detail.runtime_seconds")}
            v={fmtDuration(session.runtimeSeconds)}
          />
          <Kv k={t("detail.created")} v={fmtDate(session.createdAt)} />
          <Kv k={t("detail.updated")} v={fmtDate(session.updatedAt)} />
        </div>
      </div>
    </div>
  );
}

const CHANGE_ICON: Record<string, string> = {
  added: "+",
  modified: "~",
  deleted: "-",
  revision: "◈",
  delivery: "⎇",
};

/** Changes tab content — revisions, file deltas, deliveries. */
export function ChangesPanel({ changes }: { changes: SessionChange[] }) {
  const { t } = useI18n();
  if (changes.length === 0) {
    return <div className="empty">{t("session.no_changes")}</div>;
  }
  return (
    <div className="card" data-testid="changes-panel">
      {changes.map((c) => (
        <div className="change-row" key={c.id}>
          <span className={`c-icon ${c.changeType ?? c.kind}`} aria-hidden="true">
            {CHANGE_ICON[c.changeType ?? c.kind] ?? "•"}
          </span>
          <span className="grow">
            {c.url ? (
              <a href={c.url} target="_blank" rel="noreferrer">{c.summary}</a>
            ) : (
              c.summary
            )}
          </span>
          <span className="faint small">{fmtDate(c.ts)}</span>
        </div>
      ))}
    </div>
  );
}

import type { Session } from "../api/types";
import { useI18n } from "../i18n";
import { shortSha } from "./ChangesPanel";

function fmtDate(isoStr: string): string {
  const d = new Date(isoStr);
  if (Number.isNaN(d.getTime()) || !isoStr) return "—";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
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
export function SessionMeta({
  session,
  revisionN,
}: {
  session: Session;
  /** Latest revision sequence — the Changes tab's "revision id". */
  revisionN?: number;
}) {
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
          {session.changes?.baseSha && (
            <Kv
              k={t("detail.base")}
              v={<span className="mono">{shortSha(session.changes.baseSha)}</span>}
            />
          )}
          {session.changes?.headSha && (
            <Kv
              k={t("detail.head")}
              v={<span className="mono">{shortSha(session.changes.headSha)}</span>}
            />
          )}
          {revisionN != null && (
            <Kv k={t("detail.revision")} v={`#${revisionN}`} />
          )}
          {(session.changes?.branch || session.delivery?.branch) && (
            <Kv
              k={t("detail.branch")}
              v={
                <span className="mono">
                  {session.delivery?.branch ?? session.changes?.branch}
                </span>
              }
            />
          )}
          <Kv k={t("detail.effort")} v={session.effort ?? t("detail.none")} />
          <Kv
            k={t("detail.delivery")}
            v={session.delivery?.mode ?? t("detail.none")}
          />
          {session.delivery?.prUrl && (
            <Kv
              k={t("detail.pr")}
              v={
                <a href={session.delivery.prUrl} target="_blank" rel="noreferrer">
                  {session.delivery.prNumber
                    ? `#${session.delivery.prNumber}`
                    : session.delivery.prUrl}
                  {session.delivery.prState ? ` (${session.delivery.prState})` : ""}
                </a>
              }
            />
          )}
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
          <Kv k={t("detail.turns")} v={session.turnCount} />
          <Kv k={t("detail.created")} v={fmtDate(session.createdAt)} />
          <Kv k={t("detail.updated")} v={fmtDate(session.updatedAt)} />
        </div>
      </div>
    </div>
  );
}



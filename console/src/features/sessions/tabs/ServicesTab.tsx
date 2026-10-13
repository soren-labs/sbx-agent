import { useState } from "react";
import { ExternalLink } from "lucide-react";
import type { ServiceItem } from "../../../api/types";
import { useI18n } from "../../../i18n";
import { useApi } from "../../../state/context";
import { useQuery } from "../../../state/query";
import { Button } from "../../../ui/Button";
import { EmptyState } from "../../../ui/EmptyState";
import { ErrorNotice } from "../../../ui/ErrorNotice";
import { Pill } from "../../../ui/Pill";
import { Skeleton } from "../../../ui/Skeleton";
import { useAction } from "../../../ui/useAction";

function ServiceRow({ s, sessionId, onChanged }: { s: ServiceItem; sessionId: string; onChanged: () => void }) {
  const { t } = useI18n();
  const api = useApi();
  const [logs, setLogs] = useState(false);
  const logQ = useQuery(logs ? ["service-logs", sessionId, s.name] : null, () => api.services.logs(sessionId, s.name));
  const toggle = useAction(async (key, on: boolean) => {
    await (on ? api.services.activate(sessionId, s.name, { idempotencyKey: key }) : api.services.stop(sessionId, s.name, { idempotencyKey: key }));
    onChanged();
  });
  const previewUrl = typeof s.preview === "string" && /^https:\/\//.test(s.preview) ? s.preview : null;
  return (
    <li className="svc" aria-label={s.name}>
      <div className="svc__head">
        <strong className="svc__name">{s.name}</strong>
        <Pill tone={s.state === "running" || s.state === "ready" ? "ok" : "dim"}>{s.state}</Pill>
        <span className="faint small">
          {t("services.desired")}: {s.desired}
          {s.port ? ` · :${s.port}` : ""}
        </span>
      </div>
      {s.preview ? (
        previewUrl ? (
          <a className="svc__link" href={previewUrl} target="_blank" rel="noopener noreferrer">
            <ExternalLink size={12} /> {t("services.open_preview")}
          </a>
        ) : (
          <p className="faint small">{t("services.preview_origin")}</p>
        )
      ) : null}
      <ErrorNotice error={toggle.error} />
      <div className="svc__actions">
        <Button size="sm" variant="secondary" disabled={toggle.pending} onClick={() => void toggle.run(true)}>
          {t("services.start")}
        </Button>
        <Button size="sm" variant="secondary" disabled={toggle.pending} onClick={() => void toggle.run(false)}>
          {t("services.stop")}
        </Button>
        <Button size="sm" variant="ghost" aria-expanded={logs} onClick={() => setLogs(!logs)}>
          {t("services.logs")}
        </Button>
      </div>
      {logs ? (
        <>
          <ErrorNotice error={logQ.error} onRetry={logQ.refetch} />
          {logQ.data ? (
            <pre className="svc__logs mono" aria-label={t("services.logs")}>
              {logQ.data.lines.slice(-500).join("\n")}
            </pre>
          ) : null}
        </>
      ) : null}
    </li>
  );
}

export function ServicesTab({ sessionId, revision = 0 }: { sessionId: string; revision?: number }) {
  const { t } = useI18n();
  const api = useApi();
  const q = useQuery(["services", sessionId, revision], () => api.services.list(sessionId));
  return (
    <div>
      {q.loading && !q.data ? <Skeleton height={66} /> : null}
      <ErrorNotice error={q.error} onRetry={q.refetch} />
      {q.data && !q.data.items.length ? <EmptyState title={t("services.none")} /> : null}
      <ul className="svc__list">
        {(q.data?.items ?? []).map((s) => (
          <ServiceRow key={s.name} s={s} sessionId={sessionId} onChanged={q.refetch} />
        ))}
      </ul>
    </div>
  );
}

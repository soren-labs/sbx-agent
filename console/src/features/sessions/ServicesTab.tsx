import { useState } from "react";
import type { ServiceItem } from "../../api/types";
import { Empty, ErrorNotice, Loading, Pill, useAction } from "../../components/ui";
import { useI18n } from "../../i18n";
import { useApi } from "../../state/context";
import { useQuery } from "../../state/query";

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
    <li className="card service" aria-label={s.name}>
      <div className="row wrap">
        <strong className="grow">{s.name}</strong>
        <Pill tone={s.state === "running" || s.state === "ready" ? "ok" : "dim"}>{s.state}</Pill>
        <span className="faint small">
          {t("services.desired")}: {s.desired}
          {s.port ? ` · :${s.port}` : ""}
        </span>
      </div>
      {s.preview ? (
        previewUrl ? (
          <a href={previewUrl} target="_blank" rel="noopener noreferrer">
            {t("services.open_preview")}
          </a>
        ) : (
          <p className="faint small">{t("services.preview_origin")}</p>
        )
      ) : null}
      <ErrorNotice error={toggle.error} />
      <div className="row wrap">
        <button type="button" className="btn btn-sm" disabled={toggle.pending} onClick={() => void toggle.run(true)}>
          {t("services.start")}
        </button>
        <button type="button" className="btn btn-sm" disabled={toggle.pending} onClick={() => void toggle.run(false)}>
          {t("services.stop")}
        </button>
        <button type="button" className="btn btn-sm btn-ghost" aria-expanded={logs} onClick={() => setLogs(!logs)}>
          {t("services.logs")}
        </button>
      </div>
      {logs ? (
        <>
          <ErrorNotice error={logQ.error} onRetry={logQ.refetch} />
          {logQ.data ? <pre aria-label={t("services.logs")}>{logQ.data.lines.slice(-500).join("\n")}</pre> : null}
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
      {q.loading && !q.data ? <Loading /> : null}
      <ErrorNotice error={q.error} onRetry={q.refetch} />
      {q.data && !q.data.items.length ? <Empty>{t("services.none")}</Empty> : null}
      <ul className="plain-list">
        {(q.data?.items ?? []).map((s) => (
          <ServiceRow key={s.name} s={s} sessionId={sessionId} onChanged={q.refetch} />
        ))}
      </ul>
    </div>
  );
}

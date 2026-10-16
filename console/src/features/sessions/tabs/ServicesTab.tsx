import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type { ServiceItem, ServiceList } from "../../../api/types";
import { useI18n } from "../../../i18n";
import { useApi } from "../../../state/context";
import { useQuery } from "../../../state/query";
import { Button } from "../../../ui/Button";
import { EmptyState } from "../../../ui/EmptyState";
import { ErrorNotice } from "../../../ui/ErrorNotice";
import { Pill } from "../../../ui/Pill";
import { Skeleton } from "../../../ui/Skeleton";
import { useAction } from "../../../ui/useAction";

const UP = ["ready", "running", "starting"];

/** Desired state differs from what the runtime last reported on live compute: a Job is on it. */
const settling = (s: ServiceItem) =>
  !!s.lease_live && (s.desired === "running" ? !["ready", "running", "failed", "degraded"].includes(s.state) : UP.includes(s.state));

function ServiceRow({
  s,
  sessionId,
  preview,
  onChanged,
}: {
  s: ServiceItem;
  sessionId: string;
  preview: ServiceList["capabilities"];
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const api = useApi();
  const [logs, setLogs] = useState(false);
  const logQ = useQuery(logs ? ["service-logs", sessionId, s.name] : null, () => api.services.logs(sessionId, s.name), {
    pollMs: logs && s.lease_live ? 3000 : undefined,
  });
  const toggle = useAction(async (key, on: boolean) => {
    await (on ? api.services.activate(sessionId, s.name, { idempotencyKey: key }) : api.services.stop(sessionId, s.name, { idempotencyKey: key }));
    onChanged();
  });
  const previewOn = preview?.preview?.enabled === true && s.preview_available !== false;
  const previewUrl = previewOn && typeof s.preview === "string" && /^https:\/\//.test(s.preview) ? s.preview : null;
  const running = s.desired === "running";
  return (
    <li className="svc" aria-label={s.name}>
      <div className="svc__head">
        <strong className="svc__name">{s.name}</strong>
        <Pill tone={s.state === "running" || s.state === "ready" ? "ok" : s.state === "failed" ? "err" : "dim"}>{s.state}</Pill>
        <span className="faint small">
          {t("services.desired")}: {s.desired}
          {s.port ? ` · :${s.port}` : ""}
        </span>
      </div>
      {!s.lease_live ? <p className="faint small">{t(running ? "services.offline_running" : "services.offline")}</p> : null}
      {s.preview ? (
        previewUrl ? (
          <a className="svc__link" href={previewUrl} target="_blank" rel="noopener noreferrer">
            {t("services.open_preview")}
          </a>
        ) : (
          <p className="faint small">{t("services.preview_unavailable")}</p>
        )
      ) : null}
      <ErrorNotice error={toggle.error} />
      <div className="svc__actions">
        <Button size="sm" variant="secondary" disabled={toggle.pending || (running && UP.includes(s.state))} onClick={() => void toggle.run(true)}>
          {t("services.start")}
        </Button>
        <Button size="sm" variant="secondary" disabled={toggle.pending || (!running && !UP.includes(s.state))} onClick={() => void toggle.run(false)}>
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
            logQ.data.started === false ? (
              <p className="muted small">{t("services.logs_not_started")}</p>
            ) : logQ.data.lines.length ? (
              <pre className="svc__logs mono" aria-label={t("services.logs")}>
                {logQ.data.lines.slice(-500).join("\n")}
              </pre>
            ) : (
              <p className="muted small">{t("services.logs_empty")}</p>
            )
          ) : null}
        </>
      ) : null}
    </li>
  );
}

/** Declared services only: the agent's or a terminal's own background processes are not listed. */
export function ServicesTab({ sessionId, revision = 0 }: { sessionId: string; revision?: number }) {
  const { t } = useI18n();
  const api = useApi();
  const [poll, setPoll] = useState(false);
  const q = useQuery(["services", sessionId], () => api.services.list(sessionId), { pollMs: poll ? 2000 : undefined });
  // Service events refresh the list in place: re-keying would remount rows and close open logs.
  const seen = useRef(revision);
  useEffect(() => {
    if (seen.current === revision) return;
    seen.current = revision;
    void q.refetch();
  }, [revision, q]);
  const items = q.data?.items ?? [];
  const busy = items.some(settling);
  useEffect(() => setPoll(busy), [busy]);
  const src = q.data?.source;
  const project = src?.kind === "project_version" ? src : null;
  const outdated = project && project.current_version_id && project.current_version_id !== project.project_version_id;
  return (
    <div>
      {q.loading && !q.data ? <Skeleton height={66} /> : null}
      <ErrorNotice error={q.error} onRetry={q.refetch} />
      {q.data && project ? (
        <p className="faint small svc__source">
          {t("services.source", { project: project.project_name ?? project.project_id ?? "", n: project.project_version_ordinal ?? "?" })}{" "}
          {outdated ? t("services.source_outdated") : null} <Link to="/projects">{t("services.edit_in_projects")}</Link>
        </p>
      ) : null}
      {q.data && !items.length ? (
        <EmptyState
          title={t(project ? "services.none_declared" : "services.none_projectless")}
          body={t("services.none_body")}
          action={
            <Link className="btn btn--secondary btn--sm" to="/projects">
              {t("services.open_projects")}
            </Link>
          }
        />
      ) : null}
      <ul className="svc__list">
        {items.map((s) => (
          <ServiceRow key={s.name} s={s} sessionId={sessionId} preview={q.data?.capabilities} onChanged={q.refetch} />
        ))}
      </ul>
    </div>
  );
}

import { useState, type FormEvent } from "react";
import type { ChangeSet } from "../../api/types";
import { isApiError } from "../../api/errors";
import { Empty, ErrorNotice, Field, Loading, Pill, shortId, useAction, when } from "../../components/ui";
import { useI18n } from "../../i18n";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";
import { DeliveryCard } from "./DeliveryCard";

export function DiffView({ diff, truncated }: { diff: string; truncated?: boolean }) {
  const { t } = useI18n();
  return (
    <div>
      <pre className="diff" aria-label={t("changes.diff")}>
        {diff.split("\n").map((line, i) => (
          <span key={i} className={line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : ""}>
            {line + "\n"}
          </span>
        ))}
      </pre>
      {truncated ? <p className="faint small">{t("changes.diff_truncated")}</p> : null}
    </div>
  );
}

function ChangeSetDetail({ cs, rev }: { cs: ChangeSet; rev: number }) {
  const { t } = useI18n();
  const api = useApi();
  const qc = useQueryClient();
  const [showDiff, setShowDiff] = useState(false);
  const [title, setTitle] = useState("");
  const [draft, setDraft] = useState(true);
  const detail = useQuery(["changeset", cs.id, rev], () => api.changes.get(cs.id));
  const diff = useQuery(showDiff ? ["changeset-diff", cs.id, cs.subject_digest ?? ""] : null, () => api.changes.diff(cs.id));
  const deliveries = useQuery(["deliveries", cs.id, rev], () => api.deliveries.list(cs.id));
  const reload = () => qc.invalidate(["deliveries", cs.id]);
  const request = useAction(async (key) => {
    await api.deliveries.request(cs.id, { ...(title.trim() ? { title: title.trim() } : {}), draft }, { idempotencyKey: key });
    reload();
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void request.run();
  };
  const files = detail.data?.files ?? [];
  return (
    <div className="changeset-detail">
      <h3>{t("changes.files_title", { n: files.length })}</h3>
      <ErrorNotice error={detail.error} onRetry={detail.refetch} />
      <ul className="file-list small">
        {files.map((f) => (
          <li key={f.path}>
            <code>{f.path}</code> <span className="faint">{f.type}</span>
          </li>
        ))}
      </ul>
      <button type="button" className="btn btn-sm" aria-expanded={showDiff} onClick={() => setShowDiff(!showDiff)}>
        {showDiff ? t("changes.hide_diff") : t("changes.show_diff")}
      </button>
      {showDiff && diff.loading ? <Loading /> : null}
      <ErrorNotice error={diff.error} onRetry={diff.refetch} />
      {diff.data ? <DiffView diff={diff.data.diff} truncated={diff.data.truncated} /> : null}

      <h3>{t("delivery.heading")}</h3>
      {(deliveries.data?.items ?? []).map((d) => (
        <DeliveryCard key={d.id} delivery={d} onChanged={reload} />
      ))}
      {deliveries.data && !deliveries.data.items.length ? <p className="muted small">{t("delivery.none")}</p> : null}
      <form className="row wrap" onSubmit={submit} aria-label={t("delivery.request")}>
        <Field id={`dtitle-${cs.id}`} label={t("delivery.pr_title")}>
          <input id={`dtitle-${cs.id}`} value={title} onChange={(e) => setTitle(e.target.value)} />
        </Field>
        <label className="small row">
          <input type="checkbox" checked={draft} onChange={(e) => setDraft(e.target.checked)} />
          {t("delivery.draft")}
        </label>
        <button type="submit" className="btn btn-primary btn-sm" disabled={request.pending || cs.state !== "ready"}>
          {t("delivery.request")}
        </button>
      </form>
      <ErrorNotice error={request.error} />
    </div>
  );
}

/** Live observation (never wakes compute) + captured ChangeSets + Deliveries. */
export function ChangesPanel({ sessionId, revision = 0 }: { sessionId: string; revision?: number }) {
  const { t } = useI18n();
  const api = useApi();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const liveQ = useQuery(["changes-live", sessionId, revision], () => api.changes.live(sessionId));
  const sets = useQuery(["changesets", sessionId, revision], () => api.changes.list(sessionId));
  const capture = useAction(async (key, origin: "explicit" | "salvage") => {
    const r = await api.changes.capture(sessionId, { origin }, { idempotencyKey: key });
    qc.invalidate(["changesets", sessionId]);
    setSelected(r.changeset.id);
  });
  const unavailable = isApiError(liveQ.error) && liveQ.error.code === "executor_unavailable";
  const items = sets.data?.items ?? [];
  const current = items.find((c) => c.id === selected) ?? items[0];

  return (
    <div className="changes-panel">
      <section aria-labelledby="live-h">
        <h2 id="live-h">{t("changes.live")}</h2>
        {liveQ.loading && !liveQ.data ? <Loading /> : null}
        {unavailable ? (
          <div className="notice warn" role="status">
            <div className="grow">
              <div className="n-title">{t("changes.unavailable")}</div>
              <div className="n-body">{t("changes.unavailable_body")}</div>
            </div>
          </div>
        ) : (
          <ErrorNotice error={liveQ.error} onRetry={liveQ.refetch} />
        )}
        {liveQ.data ? (
          liveQ.data.observation.files.length ? (
            <ul className="file-list small">
              {liveQ.data.observation.files.map((f) => (
                <li key={f.path}>
                  <span className="mono faint">{f.status}</span> <code>{f.path}</code>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted small">{t("changes.clean")}</p>
          )
        ) : null}
        <div className="row wrap">
          <button type="button" className="btn btn-sm" disabled={capture.pending} onClick={() => void capture.run("explicit")}>
            {t("changes.capture")}
          </button>
          <button type="button" className="btn btn-sm btn-ghost" disabled={capture.pending} onClick={() => void capture.run("salvage")}>
            {t("changes.salvage")}
          </button>
        </div>
        <ErrorNotice error={capture.error} />
      </section>

      <section aria-labelledby="cs-h">
        <h2 id="cs-h">{t("changes.changesets")}</h2>
        <ErrorNotice error={sets.error} onRetry={sets.refetch} />
        {sets.data && !items.length ? <Empty>{t("changes.none")}</Empty> : null}
        <ul className="cs-list" aria-label={t("changes.changesets")}>
          {items.map((c) => (
            <li key={c.id}>
              <button type="button" className={`chip${current?.id === c.id ? " active" : ""}`} aria-pressed={current?.id === c.id} onClick={() => setSelected(c.id)}>
                {shortId(c.id)} · {c.origin} · {c.state}
              </button>
              <span className="faint small"> {c.file_count ?? 0} {t("changes.files")} · {when(c.created_at)}</span>
              {c.state !== "ready" && c.error ? <Pill tone="err">{t("changes.capture_failed")}</Pill> : null}
            </li>
          ))}
        </ul>
        {current ? <ChangeSetDetail key={current.id} cs={current} rev={revision} /> : null}
      </section>
    </div>
  );
}

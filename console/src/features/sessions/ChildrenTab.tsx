import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import type { Delegation, DelegationRole } from "../../api/types";
import { Empty, ErrorNotice, Field, Loading, Pill, shortId, useAction, type Tone } from "../../components/ui";
import { useI18n } from "../../i18n";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";

const ROLES: DelegationRole[] = ["review", "test", "research", "security", "integration"];
const DONE = ["completed", "succeeded", "failed", "cancelled"];
const tone = (s: string): Tone => (s === "completed" || s === "succeeded" ? "ok" : s === "failed" ? "err" : s === "cancelled" ? "dim" : "run");

function valueText(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  const s = (v as { summary?: unknown }).summary;
  return typeof s === "string" ? s : JSON.stringify(v, null, 2);
}

function DelegationCard({ g, onChanged }: { g: Delegation; onChanged: () => void }) {
  const { t } = useI18n();
  const api = useApi();
  const wait = useAction(async (key) => {
    await api.delegations.wait(g.id, { idempotencyKey: key });
    onChanged();
  });
  const cancel = useAction(async (key) => {
    await api.delegations.cancel(g.id, { idempotencyKey: key });
    onChanged();
  });
  const r = g.result;
  return (
    <li className="card" aria-label={`${g.role} ${shortId(g.id)}`}>
      <div className="row wrap">
        <strong className="grow">{g.role}</strong>
        <Pill tone={tone(g.state)}>{g.state}</Pill>
      </div>
      <div className="small muted">
        <Link to={`/sessions/${g.child_session_id}`}>{t("children.open_child")}</Link>
        {g.subject.changeset_id ? ` · ${t("children.subject")}: ${shortId(g.subject.changeset_id)}` : ""}
      </div>
      {r ? (
        <div className="result small" aria-label={t("children.result")}>
          <div className="row wrap">
            <span>
              {r.kind}: <strong>{r.verdict ?? "—"}</strong>
            </span>
            <Pill tone={r.independent ? "ok" : "warn"}>{r.independent ? t("children.independent") : t("children.not_independent")}</Pill>
            {r.validation_status ? <Pill tone={r.validation_status === "valid" ? "ok" : "warn"}>{r.validation_status}</Pill> : null}
          </div>
          {valueText(r.value) ? <pre>{valueText(r.value)}</pre> : null}
        </div>
      ) : (
        <p className="faint small">{t("children.no_result")}</p>
      )}
      <ErrorNotice error={wait.error ?? cancel.error} />
      {!DONE.includes(g.state) ? (
        <div className="row wrap">
          <button type="button" className="btn btn-sm" disabled={wait.pending} onClick={() => void wait.run()}>
            {t("children.wait")}
          </button>
          <button type="button" className="btn btn-sm btn-danger" disabled={cancel.pending} onClick={() => void cancel.run()}>
            {t("children.cancel")}
          </button>
        </div>
      ) : null}
    </li>
  );
}

export function ChildrenTab({ sessionId, revision = 0 }: { sessionId: string; revision?: number }) {
  const { t } = useI18n();
  const api = useApi();
  const qc = useQueryClient();
  const [role, setRole] = useState<DelegationRole>("review");
  const [changesetId, setChangesetId] = useState("");
  const [instructions, setInstructions] = useState("");
  const list = useQuery(["delegations", sessionId, revision], () => api.delegations.list(sessionId), { pollMs: 8000 });
  const sets = useQuery(["changesets", sessionId, revision], () => api.changes.list(sessionId));
  const ready = (sets.data?.items ?? []).filter((c) => c.state === "ready");
  const subject = changesetId || ready[0]?.id || "";
  const spawn = useAction(async (key) => {
    await api.delegations.spawn(
      sessionId,
      { role, ...(subject ? { changeset_id: subject } : {}), ...(instructions.trim() ? { instructions: instructions.trim() } : {}) },
      { idempotencyKey: key },
    );
    setInstructions("");
    qc.invalidate(["delegations", sessionId]);
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void spawn.run();
  };
  return (
    <div>
      <form className="card" onSubmit={submit} aria-label={t("children.spawn")}>
        <div className="controls">
          <Field id="child-role" label={t("children.role")}>
            <select id="child-role" value={role} onChange={(e) => setRole(e.target.value as DelegationRole)}>
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </Field>
          <Field id="child-cs" label={t("children.subject")}>
            <select id="child-cs" value={subject} onChange={(e) => setChangesetId(e.target.value)}>
              <option value="">{t("children.no_subject")}</option>
              {ready.map((c) => (
                <option key={c.id} value={c.id}>
                  {shortId(c.id)} · {c.origin}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field id="child-instr" label={t("children.instructions")}>
          <textarea id="child-instr" rows={2} value={instructions} onChange={(e) => setInstructions(e.target.value)} />
        </Field>
        <ErrorNotice error={spawn.error} />
        <button type="submit" className="btn btn-primary btn-sm" disabled={spawn.pending}>
          {t("children.spawn_role", { role })}
        </button>
      </form>
      {list.loading && !list.data ? <Loading /> : null}
      <ErrorNotice error={list.error} onRetry={list.refetch} />
      {list.data && !list.data.items.length ? <Empty>{t("children.none")}</Empty> : null}
      <ul className="plain-list">
        {(list.data?.items ?? []).map((g) => (
          <DelegationCard key={g.id} g={g} onChanged={() => qc.invalidate(["delegations", sessionId])} />
        ))}
      </ul>
    </div>
  );
}

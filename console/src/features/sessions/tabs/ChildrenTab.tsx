import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import type { Delegation, DelegationRole } from "../../../api/types";
import { useI18n } from "../../../i18n";
import { useApi, useQueryClient } from "../../../state/context";
import { useQuery } from "../../../state/query";
import { Button } from "../../../ui/Button";
import { EmptyState } from "../../../ui/EmptyState";
import { ErrorNotice } from "../../../ui/ErrorNotice";
import { Field, Select, Textarea } from "../../../ui/Field";
import { Pill, type Tone } from "../../../ui/Pill";
import { Skeleton } from "../../../ui/Skeleton";
import { useAction } from "../../../ui/useAction";
import { shortId } from "../../../ui/format";

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
    <li className="child" aria-label={`${g.role} ${shortId(g.id)}`}>
      <div className="child__head">
        <strong>{g.role}</strong>
        <Pill tone={tone(g.state)}>{g.state}</Pill>
        <span className="files__spacer" />
        <Link to={`/sessions/${g.child_session_id}`}>{t("children.open_child")}</Link>
      </div>
      <div className="small muted">
        {g.subject.changeset_id ? `${t("children.subject")}: ${shortId(g.subject.changeset_id)}` : null}
      </div>
      {r ? (
        <div className="child__result small" aria-label={t("children.result")}>
          <div className="child__head">
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
        <div className="child__actions">
          <Button size="sm" variant="secondary" disabled={wait.pending} onClick={() => void wait.run()}>
            {t("children.wait")}
          </Button>
          <Button size="sm" variant="danger" disabled={cancel.pending} onClick={() => void cancel.run()}>
            {t("children.cancel")}
          </Button>
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
    <div className="children">
      <form className="child-form" onSubmit={submit} aria-label={t("children.spawn")}>
        <div className="child-form__row">
          <Field label={t("children.role")} htmlFor="child-role">
            <Select id="child-role" value={role} onChange={(e) => setRole(e.target.value as DelegationRole)}>
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t("children.subject")} htmlFor="child-cs">
            <Select id="child-cs" value={subject} onChange={(e) => setChangesetId(e.target.value)}>
              <option value="">{t("children.no_subject")}</option>
              {ready.map((c) => (
                <option key={c.id} value={c.id}>
                  {shortId(c.id)} · {c.origin}
                </option>
              ))}
            </Select>
          </Field>
        </div>
        <Field label={t("children.instructions")} htmlFor="child-instr">
          <Textarea id="child-instr" rows={2} value={instructions} onChange={(e) => setInstructions(e.target.value)} />
        </Field>
        <ErrorNotice error={spawn.error} />
        <Button type="submit" size="sm" variant="primary" disabled={spawn.pending}>
          {t("children.spawn_role", { role })}
        </Button>
      </form>
      {list.loading && !list.data ? <Skeleton height={66} /> : null}
      <ErrorNotice error={list.error} onRetry={list.refetch} />
      {list.data && !list.data.items.length ? <EmptyState title={t("children.none")} /> : null}
      <ul className="child__list">
        {(list.data?.items ?? []).map((g) => (
          <DelegationCard key={g.id} g={g} onChanged={() => qc.invalidate(["delegations", sessionId])} />
        ))}
      </ul>
    </div>
  );
}

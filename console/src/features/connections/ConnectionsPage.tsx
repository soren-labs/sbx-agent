import { useEffect, useState } from "react";
import type { Connection, ConnectionCredential } from "../../api/types";
import { Icon } from "../../components/icons";
import { ErrorNotice, Loading, Pill, healthTone, useAction, when } from "../../components/ui";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useAuth } from "../../state/auth";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";
import { useDocumentTitle } from "../../state/title";
import { ConnectionForm } from "./ConnectionForm";
import { KINDS, type KindMeta } from "./kinds";

export function useConnections() {
  const api = useApi();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const [verifying, setVerifying] = useState(false);
  const q = useQuery(w ? ["connections", w] : null, () => api.connections.list(w!), {
    pollMs: verifying ? 3000 : undefined,
  });
  const any = q.data?.items.some((c) => c.health === "verifying") ?? false;
  useEffect(() => setVerifying(any), [any]);
  return q;
}

function ConnectionCard({ c, onChanged }: { c: Connection; onChanged: () => void }) {
  const { t } = useI18n();
  const api = useApi();
  const [replacing, setReplacing] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const validate = useAction(async (key) => {
    await api.connections.validate(c.id, { idempotencyKey: key });
    onChanged();
  });
  const replace = useAction(async (key, credential: ConnectionCredential) => {
    await api.connections.replaceCredential(c.id, { credential, expected_version: c.version }, { idempotencyKey: key });
    setReplacing(false);
    onChanged();
  });
  const disconnect = useAction(async (key) => {
    await api.connections.disconnect(c.id, { idempotencyKey: key });
    setConfirm(false);
    onChanged();
  });
  const models = c.catalog?.models ?? [];
  const free = models.filter((m) => m.free);
  const needsAttention = c.health === "degraded" || c.health === "reauth_required";

  return (
    <div className="card conn-card" role="group" aria-label={c.label}>
      <div className="row wrap">
        <strong className="grow">{c.label}</strong>
        <Pill tone={healthTone(c.health)}>{t(`health.${c.health}` as I18nKey)}</Pill>
      </div>
      <dl className="kv small">
        {c.external_identity ? (
          <>
            <dt>{t("conn.identity")}</dt>
            <dd>{c.external_identity}</dd>
          </>
        ) : null}
        {c.health_reason ? (
          <>
            <dt>{t("conn.reason")}</dt>
            <dd>{c.health_reason}</dd>
          </>
        ) : null}
        {c.credential ? (
          <>
            <dt>{t("conn.credential")}</dt>
            <dd>
              v{c.credential.ordinal} · {when(c.credential.created_at)}
            </dd>
          </>
        ) : null}
        {c.validation ? (
          <>
            <dt>{t("conn.validation")}</dt>
            <dd>
              {c.validation.status} · {when(c.validation.observed_at)}
            </dd>
          </>
        ) : null}
        {c.catalog?.preferred_model ? (
          <>
            <dt>{t("conn.preferred_model")}</dt>
            <dd>
              <code>{c.catalog.preferred_model}</code>
              {models.find((m) => m.id === c.catalog?.preferred_model)?.free ? (
                <span className="faint"> · {t("conn.free")}</span>
              ) : null}
            </dd>
          </>
        ) : null}
        {free.length ? (
          <>
            <dt>{t("conn.free_models")}</dt>
            <dd>{free.map((m) => m.id).join(", ")}</dd>
          </>
        ) : null}
      </dl>
      <ErrorNotice error={validate.error ?? replace.error ?? disconnect.error} />
      <div className="row wrap">
        <button type="button" className="btn btn-sm" disabled={validate.pending} onClick={() => void validate.run()}>
          {needsAttention ? t("conn.retry_validation") : t("conn.validate")}
        </button>
        <button type="button" className="btn btn-sm" aria-expanded={replacing} onClick={() => setReplacing(!replacing)}>
          {t("conn.replace_credential")}
        </button>
        {confirm ? (
          <>
            <button type="button" className="btn btn-sm btn-danger" disabled={disconnect.pending} onClick={() => void disconnect.run()}>
              {t("conn.confirm_disconnect")}
            </button>
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => setConfirm(false)}>
              {t("common.cancel")}
            </button>
          </>
        ) : (
          <button type="button" className="btn btn-sm btn-danger" onClick={() => setConfirm(true)}>
            {t("conn.disconnect")}
          </button>
        )}
      </div>
      {replacing ? (
        <ConnectionForm kind={c.kind} mode="replace" pending={replace.pending} onSubmit={(cred) => void replace.run(cred)} />
      ) : null}
    </div>
  );
}

function KindSection({ meta, items, onChanged }: { meta: KindMeta; items: Connection[]; onChanged: () => void }) {
  const { t } = useI18n();
  const api = useApi();
  const { workspace } = useAuth();
  const create = useAction(async (key, credential: ConnectionCredential, label: string) => {
    await api.connections.create(workspace!.id, { kind: meta.kind, label, credential }, { idempotencyKey: key });
    onChanged();
  });
  return (
    <section className="card provider-section" aria-labelledby={`kind-${meta.kind}`}>
      <div className="row wrap">
        <span className="provider-icon" aria-hidden="true"><Icon name={meta.kind === "github" ? "github" : meta.kind === "modal" ? "terminal" : "sparkles"} size={20} /></span>
        <h2 id={`kind-${meta.kind}`} className="grow">
          {t(meta.title)}
        </h2>
        <Pill tone={meta.required ? "run" : "dim"}>{meta.required ? t("setup.required") : t("setup.optional")}</Pill>
      </div>
      <p className="muted small">{t(meta.purpose)}</p>
      {items.map((c) => (
        <ConnectionCard key={c.id} c={c} onChanged={onChanged} />
      ))}
      <ErrorNotice error={create.error} />
      <details className="adv-toggle" open={items.length === 0}>
        <summary>{t("conn.add")}</summary>
        <ConnectionForm kind={meta.kind} mode="create" pending={create.pending} onSubmit={(cred, label) => void create.run(cred, label)} />
      </details>
    </section>
  );
}

export function ConnectionsPage() {
  const { t } = useI18n();
  useDocumentTitle(t("nav.connections"));
  const qc = useQueryClient();
  const q = useConnections();
  const changed = () => qc.invalidate(["connections"]);
  const items = (q.data?.items ?? []).filter((c) => c.state !== "revoked");
  return (
    <div className="narrow">
      <h1>{t("nav.connections")}</h1>
      <p className="muted">{t("conn.intro")}</p>
      {q.loading && !q.data ? <Loading /> : null}
      <ErrorNotice error={q.error} onRetry={q.refetch} />
      {q.data
        ? KINDS.map((m) => (
            <KindSection key={m.kind} meta={m} items={items.filter((c) => c.kind === m.kind)} onChanged={changed} />
          ))
        : null}
    </div>
  );
}

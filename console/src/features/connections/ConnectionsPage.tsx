import { useEffect, useState, type ReactNode } from "react";
import type { Connection, ConnectionCredential, ConnectionKind, Harness, InferenceProtocol } from "../../api/types";
import { Icon } from "../../components/icons";
import { ErrorNotice, Loading, useAction, when } from "../../components/ui";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useAuth } from "../../state/auth";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";
import { useDocumentTitle } from "../../state/title";
import { PROTOCOL_NAMES, PROTOCOLS } from "../sessions/harnesses";
import { ConnectionForm } from "./ConnectionForm";
import { InferenceForm } from "./InferenceForm";

const settling = (c: Connection) => c.state === "configured" && (c.health === "verifying" || c.health === "unverified");

export function useConnections() {
  const api = useApi();
  const qc = useQueryClient();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const q = useQuery(w ? ["connections", w] : null, () => api.connections.list(w!));
  // Validation runs as a server Job: follow it until every Connection has a verdict.
  const waiting = (q.data?.items ?? []).some(settling);
  useEffect(() => {
    if (!waiting || !w) return;
    const id = setInterval(() => void qc.fetch(["connections", w], () => api.connections.list(w)), 2000);
    return () => clearInterval(id);
  }, [waiting, w, qc, api]);
  return q;
}

const BADGE: Record<string, "ok" | "warn" | "idle" | "err"> = {
  ready: "ok",
  degraded: "warn",
  verifying: "warn",
  unverified: "warn",
  reauth_required: "err",
};

/** Server reason codes with a plain-language explanation; unknown codes are shown as-is. */
const REASONS: Record<string, I18nKey> = {
  inference_rejected_key: "reason.inference_rejected_key",
  inference_endpoint_or_model_rejected: "reason.inference_endpoint_or_model_rejected",
  inference_base_url_not_public: "reason.inference_base_url_not_public",
  inference_base_url_redirects: "reason.inference_base_url_redirects",
  inference_rate_limited: "reason.rate_limited",
  rate_limited: "reason.rate_limited",
  provider_rejected_credential: "reason.provider_rejected_credential",
  modal_rejected_token: "reason.modal_rejected_token",
  github_rejected_token: "reason.github_rejected_token",
  github_no_repository_access: "reason.github_no_repository_access",
  github_rate_limited: "reason.rate_limited",
};

function HealthBadge({ conn }: { conn: Connection }) {
  const { t } = useI18n();
  return (
    <span className={`hs-badge ${BADGE[conn.health] ?? "idle"}`} data-health={conn.health}>
      <i /> {t(`health.${conn.health}` as I18nKey)}
    </span>
  );
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="conn-detail">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function ConnectionCard({
  conn,
  harnesses,
  onChanged,
}: {
  conn: Connection;
  harnesses?: Harness[];
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const api = useApi();
  const [replacing, setReplacing] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const validate = useAction(async (key) => {
    await api.connections.validate(conn.id, { idempotencyKey: key });
    onChanged();
  });
  const replace = useAction(async (key, credential: ConnectionCredential) => {
    await api.connections.replaceCredential(
      conn.id,
      { credential, expected_version: conn.version },
      { idempotencyKey: key },
    );
    setReplacing(false);
    onChanged();
  });
  const disconnect = useAction(async (key) => {
    await api.connections.disconnect(conn.id, { idempotencyKey: key });
    setConfirming(false);
    onChanged();
  });

  const reason = conn.health_reason;
  const details = (conn.validation?.details ?? {}) as Record<string, unknown>;
  const probes = (details.endpoints ?? {}) as Partial<Record<InferenceProtocol, { status?: string; http_status?: number }>>;
  const repositories = Object.entries((details.repositories ?? {}) as Record<string, { visible?: boolean; push?: boolean | null }>);
  const endpoints = conn.config?.endpoints ?? {};
  const models = conn.catalog?.models ?? (conn.config?.models ?? []).map((id) => ({ id }));
  const needsAttention = conn.health === "degraded" || conn.health === "reauth_required";

  return (
    <div className="hs-connection-instance" role="group" aria-label={conn.label} data-testid={`connection-${conn.kind}`}>
      <div className="hs-instance-header">
        <strong>{conn.label}</strong>
        <HealthBadge conn={conn} />
      </div>
      {reason && conn.health !== "ready" ? (
        <p className="hs-note warn" role="status">
          {REASONS[reason] ? t(REASONS[reason]) : null}
          <span className="conn-reason-code">
            {t("conn.reason")}: <code>{reason}</code>
          </span>
        </p>
      ) : null}
      <dl className="conn-details">
        {conn.external_identity ? <Detail label={t("conn.identity")}>{conn.external_identity}</Detail> : null}
        {conn.kind === "inference_api" ? (
          <>
            <Detail label={t("inference.model")}>
              <code>{conn.config?.model ?? "—"}</code>
              {models.length > 1 ? (
                <span className="faint"> · {t("inference.model_count", { count: models.length })}</span>
              ) : null}
            </Detail>
            {PROTOCOLS.filter((p) => endpoints[p]).map((p) => (
              <Detail key={p} label={PROTOCOL_NAMES[p]}>
                <code>{endpoints[p]}</code>
                {probes[p]?.status && probes[p]?.status !== "ready" ? (
                  <span className="conn-probe err">
                    {" "}
                    · {t("inference.probe_failed", { status: String(probes[p]?.http_status ?? probes[p]?.status) })}
                  </span>
                ) : null}
              </Detail>
            ))}
          </>
        ) : null}
        {repositories.length ? (
          <Detail label={t("conn.repositories")}>
            <ul className="hs-repo-list">
              {repositories.map(([name, access]) => (
                <li key={name}>
                  <Icon name="branch" size={12} />
                  {name}
                  <span className="faint">
                    {" "}
                    · {t(access.visible ? (access.push === false ? "conn.repo_read" : "conn.repo_write") : "conn.repo_hidden")}
                  </span>
                </li>
              ))}
            </ul>
          </Detail>
        ) : null}
        {conn.validation?.observed_at ? (
          <Detail label={t("conn.validation")}>{when(conn.validation.observed_at)}</Detail>
        ) : null}
      </dl>
      {conn.legacy ? <p className="hs-note">{t("conn.legacy_note")}</p> : null}
      <ErrorNotice error={validate.error ?? replace.error ?? disconnect.error} />
      <div className="hs-instance-actions">
        {!conn.legacy ? (
          <button type="button" className="button" disabled={validate.pending || settling(conn)} onClick={() => void validate.run()}>
            <Icon name="refresh" size={13} />
            {needsAttention ? t("conn.retry_validation") : t("conn.validate")}
          </button>
        ) : null}
        {!conn.legacy ? (
          <button type="button" className="button" aria-expanded={replacing} onClick={() => setReplacing((v) => !v)}>
            {t("conn.replace_credential")}
          </button>
        ) : null}
        {confirming ? (
          <>
            <button type="button" className="button danger" disabled={disconnect.pending} onClick={() => void disconnect.run()}>
              {t("conn.confirm_disconnect")}
            </button>
            <button type="button" className="button ghost" onClick={() => setConfirming(false)}>
              {t("common.cancel")}
            </button>
          </>
        ) : (
          <button type="button" className="button ghost danger" onClick={() => setConfirming(true)}>
            {t("conn.disconnect")}
          </button>
        )}
      </div>
      {replacing && !conn.legacy ? (
        <div className="hs-token-container">
          {conn.kind === "inference_api" ? (
            <InferenceForm
              mode="replace"
              initial={conn.config}
              harnesses={harnesses}
              pending={replace.pending}
              onSubmit={(credential) => void replace.run(credential)}
              onCancel={() => setReplacing(false)}
            />
          ) : (
            <ConnectionForm
              kind={conn.kind as "modal" | "github"}
              mode="replace"
              pending={replace.pending}
              onSubmit={(credential) => void replace.run(credential)}
            />
          )}
        </div>
      ) : null}
    </div>
  );
}

const SECTIONS: { kind: ConnectionKind; icon: string; title: I18nKey; purpose: I18nKey }[] = [
  { kind: "inference_api", icon: "sparkle", title: "kind.inference_api", purpose: "kind.inference_api.purpose" },
  { kind: "modal", icon: "cpu", title: "kind.modal", purpose: "kind.modal.purpose" },
  { kind: "github", icon: "github", title: "kind.github", purpose: "kind.github.purpose" },
];

export function ConnectionsPage() {
  const { t } = useI18n();
  useDocumentTitle(t("nav.connections"));
  const qc = useQueryClient();
  const api = useApi();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const q = useConnections();
  const harnesses = useQuery(["harnesses"], () => api.catalog.harnesses());
  const [adding, setAdding] = useState<ConnectionKind | null>(null);

  // Refetch in place: dropping the list would unmount every card (and any open form).
  const changed = () => {
    q.refetch();
    qc.invalidate(["models"]);
  };
  const items = (q.data?.items ?? []).filter((c) => c.state !== "revoked");
  const legacy = items.filter((c) => c.legacy);

  const create = useAction(async (key, kind: ConnectionKind, credential: ConnectionCredential, label: string) => {
    await api.connections.create(w!, { kind, label, credential }, { idempotencyKey: key });
    setAdding(null);
    changed();
  });

  return (
    <div className="hs-page settings-content">
      <div className="page-eyebrow">{t("conn.eyebrow")}</div>
      <h1>{t("nav.connections")}</h1>
      <p className="hs-lead">{t("conn.lead")}</p>

      {q.loading && !q.data ? <Loading /> : null}
      <ErrorNotice error={q.error ?? create.error} onRetry={q.error ? q.refetch : undefined} />

      {SECTIONS.map((section, index) => {
        const mine = items.filter((c) => c.kind === section.kind);
        const ready = mine.filter((c) => c.health === "ready").length;
        const open = adding === section.kind || (q.data !== undefined && mine.length === 0);
        return (
          <section
            key={section.kind}
            className={`settings-section hs-card ${ready ? "is-ready" : ""}`}
            aria-label={t(section.title)}
            data-testid={`section-${section.kind}`}
          >
            <div className="hs-card-head">
              <span className="hs-card-icon">
                <Icon name={section.icon} size={20} />
                <b>{index + 1}</b>
              </span>
              <div className="hs-card-title">
                <h2>{t(section.title)}</h2>
                <p>{t(section.purpose)}</p>
              </div>
              <span className={`hs-badge ${ready ? "ok" : mine.length ? "warn" : "idle"}`}>
                <i />{" "}
                {ready
                  ? t("conn.count_ready", { count: ready })
                  : mine.length
                    ? t("conn.none_ready")
                    : t("conn.not_connected")}
              </span>
            </div>
            <div className="hs-card-body">
              {mine.map((c) => (
                <ConnectionCard key={c.id} conn={c} harnesses={harnesses.data?.items} onChanged={changed} />
              ))}
              {mine.length > 0 && !open ? (
                <div className="hs-actions">
                  <button type="button" className="button" onClick={() => setAdding(section.kind)}>
                    <Icon name="plus" size={13} />
                    {t("conn.add_another")}
                  </button>
                </div>
              ) : null}
              {open ? (
                <div className="hs-token-container">
                  {section.kind === "inference_api" ? (
                    <InferenceForm
                      mode="create"
                      harnesses={harnesses.data?.items}
                      pending={create.pending}
                      onSubmit={(credential, label) => void create.run("inference_api", credential, label)}
                      onCancel={mine.length ? () => setAdding(null) : undefined}
                    />
                  ) : (
                    <ConnectionForm
                      kind={section.kind}
                      mode="create"
                      pending={create.pending}
                      onSubmit={(credential, label) => void create.run(section.kind, credential, label)}
                    />
                  )}
                </div>
              ) : null}
            </div>
          </section>
        );
      })}

      {legacy.length ? (
        <section className="settings-section hs-card" aria-label={t("conn.legacy_title")} data-testid="section-legacy">
          <div className="hs-card-head">
            <span className="hs-card-icon">
              <Icon name="clock" size={20} />
            </span>
            <div className="hs-card-title">
              <h2>{t("conn.legacy_title")}</h2>
              <p>{t("conn.legacy_lead")}</p>
            </div>
          </div>
          <div className="hs-card-body">
            {legacy.map((c) => (
              <ConnectionCard key={c.id} conn={c} onChanged={changed} />
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

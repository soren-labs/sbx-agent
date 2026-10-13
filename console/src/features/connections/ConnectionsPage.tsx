import { useState, type ReactNode } from "react";
import {
  ChevronDown,
  Cpu,
  Github,
  KeyRound,
  MoreHorizontal,
  Plug,
  RefreshCw,
} from "lucide-react";
import type {
  Connection,
  ConnectionCredential,
  ConnectionKind,
  InferenceProtocol,
} from "../../api/types";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useAuth } from "../../state/auth";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";
import { useDocumentTitle } from "../../state/title";
import { Button, IconButton } from "../../ui/Button";
import { Dialog } from "../../ui/Dialog";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { when } from "../../ui/format";
import { Menu, MenuItem } from "../../ui/Menu";
import { Pill, StatusDot, type Tone } from "../../ui/Pill";
import { Spinner } from "../../ui/Spinner";
import { useAction } from "../../ui/useAction";
import { PageHeader } from "../../shell/PageHeader";
import { PROTOCOL_NAMES, PROTOCOLS } from "../sessions/harnesses";
import { computeSetup, type SetupStatus } from "../setup/checklist";
import { ConnectionForm } from "./ConnectionForm";
import { InferenceForm } from "./InferenceForm";
import { useConnections } from "./useConnections";

const HEALTH_TONE: Record<string, Tone> = {
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

const SETUP_DOT: Record<SetupStatus, Tone> = {
  ready: "ok",
  pending: "warn",
  attention: "err",
  missing: "dim",
};

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

const KIND_TILE: Record<string, ReactNode> = {
  inference_api: <KeyRound size={16} />,
  modal: <Cpu size={16} />,
  github: <Github size={16} />,
};

function ConnectionRow({
  conn,
  onChanged,
  onReplace,
  onDisconnect,
}: {
  conn: Connection;
  onChanged: () => void;
  onReplace: (conn: Connection) => void;
  onDisconnect: (conn: Connection) => void;
}) {
  const { t } = useI18n();
  const api = useApi();
  const validate = useAction(async (key) => {
    await api.connections.validate(conn.id, { idempotencyKey: key });
    onChanged();
  });

  const reason = conn.health_reason;
  const details = (conn.validation?.details ?? {}) as Record<string, unknown>;
  const probes = (details.endpoints ?? {}) as Partial<
    Record<InferenceProtocol, { status?: string; http_status?: number }>
  >;
  const repositories = Object.entries(
    (details.repositories ?? {}) as Record<string, { visible?: boolean; push?: boolean | null }>,
  );
  const endpoints = conn.config?.endpoints ?? {};
  const models = conn.catalog?.models ?? (conn.config?.models ?? []).map((id) => ({ id }));
  const needsAttention = conn.health === "degraded" || conn.health === "reauth_required";
  const settling = conn.state === "configured" && (conn.health === "verifying" || conn.health === "unverified");

  const host = Object.values(endpoints)[0]?.replace(/^https?:\/\//, "").split("/")[0];
  const sub = [
    conn.external_identity,
    conn.kind === "inference_api" ? (host ?? conn.config?.model) : null,
    conn.kind === "inference_api" && models.length ? t("inference.model_count", { count: models.length }) : null,
    conn.kind === "github" && repositories.length
      ? `${repositories.filter(([, a]) => a.visible).length}/${repositories.length} ${t("conn.repositories").toLowerCase()}`
      : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="lrow" role="group" aria-label={conn.label} data-testid={`connection-${conn.kind}`}>
      <div className="lrow__line">
        <span className="lrow__tile" aria-hidden="true">
          {KIND_TILE[conn.kind] ?? <Plug size={16} />}
        </span>
        <div className="lrow__main">
          <span className="lrow__name">{conn.label}</span>
          {sub ? <span className="lrow__sub">{sub}</span> : null}
        </div>
        <Pill tone={HEALTH_TONE[conn.health] ?? "dim"} title={conn.health}>
          {t(`health.${conn.health}` as I18nKey)}
        </Pill>
        <div className="lrow__aside">
          {!conn.legacy ? (
            <Button
              size="sm"
              variant="secondary"
              icon={validate.pending ? <Spinner small /> : <RefreshCw size={13} />}
              disabled={validate.pending || settling}
              onClick={() => void validate.run()}
            >
              {needsAttention ? t("conn.retry_validation") : t("conn.validate")}
            </Button>
          ) : null}
          <Menu
            align="end"
            label={t("conn.actions")}
            trigger={<IconButton size="sm" label={t("conn.actions")} icon={<MoreHorizontal size={15} />} />}
          >
            {!conn.legacy ? (
              <MenuItem label={t("conn.replace_credential")} onSelect={() => onReplace(conn)} />
            ) : null}
            <MenuItem danger label={t("conn.disconnect")} onSelect={() => onDisconnect(conn)} />
          </Menu>
        </div>
      </div>
      {reason && conn.health !== "ready" ? (
        <p className="note note--warn" role="status">
          {REASONS[reason] ? t(REASONS[reason]) : null}
          <code>
            {t("conn.reason")}: {reason}
          </code>
        </p>
      ) : null}
      {conn.legacy ? <p className="note">{t("conn.legacy_note")}</p> : null}
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
                  <span className="note--err">
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
            <ul className="conn-repo-list">
              {repositories.map(([name, access]) => (
                <li key={name}>
                  <Github size={12} />
                  {name}
                  <span className="faint">
                    {" "}
                    ·{" "}
                    {t(access.visible ? (access.push === false ? "conn.repo_read" : "conn.repo_write") : "conn.repo_hidden")}
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
      <ErrorNotice error={validate.error} />
    </div>
  );
}

const SECTIONS: { kind: ConnectionKind; title: I18nKey; purpose: I18nKey }[] = [
  { kind: "inference_api", title: "kind.inference_api", purpose: "kind.inference_api.purpose" },
  { kind: "modal", title: "kind.modal", purpose: "kind.modal.purpose" },
  { kind: "github", title: "kind.github", purpose: "kind.github.purpose" },
];

export function ConnectionsPage() {
  const { t } = useI18n();
  useDocumentTitle(t("nav.connections"));
  const api = useApi();
  const qc = useQueryClient();
  const { workspace, me } = useAuth();
  const w = workspace?.id ?? null;
  const q = useConnections();
  const harnesses = useQuery(["harnesses"], () => api.catalog.harnesses());
  const [adding, setAdding] = useState<ConnectionKind | null>(null);
  const [replacing, setReplacing] = useState<Connection | null>(null);
  const [disconnecting, setDisconnecting] = useState<Connection | null>(null);
  const [legacyOpen, setLegacyOpen] = useState(false);

  // Refetch in place: dropping the list would unmount every row (and any open form).
  const changed = () => {
    q.refetch();
    qc.invalidate(["models"]);
  };
  const items = (q.data?.items ?? []).filter((c) => c.state !== "revoked");
  const legacy = items.filter((c) => c.legacy);
  const setup = computeSetup(me, items);
  const done = setup.items.filter((i) => i.status === "ready").length;

  const create = useAction(async (key, kind: ConnectionKind, credential: ConnectionCredential, label: string) => {
    await api.connections.create(w!, { kind, label, credential }, { idempotencyKey: key });
    setAdding(null);
    changed();
  });
  const replace = useAction(async (key, conn: Connection, credential: ConnectionCredential) => {
    await api.connections.replaceCredential(conn.id, { credential, expected_version: conn.version }, { idempotencyKey: key });
    setReplacing(null);
    changed();
  });
  const disconnect = useAction(async (key, conn: Connection) => {
    await api.connections.disconnect(conn.id, { idempotencyKey: key });
    setDisconnecting(null);
    changed();
  });

  return (
    <div className="page">
      <PageHeader title={t("nav.connections")} />
      <p className="page__lead">{t("conn.lead")}</p>

      {q.loading && !q.data ? (
        <div className="lcard">
          <div className="lrow__empty">
            <Spinner small /> {t("common.loading")}
          </div>
        </div>
      ) : null}
      <ErrorNotice error={q.error ?? create.error ?? replace.error ?? disconnect.error} onRetry={q.error ? q.refetch : undefined} />

      {q.data && !setup.complete ? (
        <div className="setup-card" role="status">
          <div className="setup-card__head">
            <span>{t("conn.setup_title")}</span>
            <span className="setup-card__count">
              {t("conn.setup_progress", { done, total: setup.items.length })}
            </span>
          </div>
          <ul className="setup-card__items">
            {setup.items.map((i) => (
              <li key={i.id}>
                <a href={`#conn-${i.id}`}>
                  <StatusDot tone={SETUP_DOT[i.status]} />
                  {t(`home.setup.${i.id}` as I18nKey)}
                </a>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {SECTIONS.map((section) => {
        const mine = items.filter((c) => c.kind === section.kind && !c.legacy);
        const ready = mine.filter((c) => c.health === "ready").length;
        return (
          <section
            key={section.kind}
            id={`conn-${section.kind}`}
            className="page__section"
            aria-label={t(section.title)}
            data-testid={`section-${section.kind}`}
          >
            <div className="page__section-head">
              <h2>
                {t(section.title)}{" "}
                {q.data ? (
                  <Pill tone={ready ? "ok" : mine.length ? "warn" : "dim"}>
                    {ready
                      ? t("conn.count_ready", { count: ready })
                      : mine.length
                        ? t("conn.none_ready")
                        : t("conn.not_connected")}
                  </Pill>
                ) : null}
              </h2>
              <p>{t(section.purpose)}</p>
            </div>
            <div className="lcard">
              {mine.map((c) => (
                <ConnectionRow
                  key={c.id}
                  conn={c}
                  onChanged={changed}
                  onReplace={setReplacing}
                  onDisconnect={setDisconnecting}
                />
              ))}
              <div className="lrow__empty">
                {mine.length ? t("conn.add_another") : t("conn.not_connected")}
                <Button
                  size="sm"
                  variant={mine.length ? "secondary" : "primary"}
                  onClick={() => setAdding(section.kind)}
                >
                  {section.kind === "inference_api" ? t("conn.add_key") : t("conn.connect")}
                </Button>
              </div>
            </div>
          </section>
        );
      })}

      {legacy.length ? (
        <section className="page__section" aria-label={t("conn.legacy_title")} data-testid="section-legacy">
          <div className="page__section-head">
            <h2>
              <button
                type="button"
                className="mc-group-head"
                aria-expanded={legacyOpen}
                onClick={() => setLegacyOpen((v) => !v)}
              >
                <ChevronDown size={14} style={{ transform: legacyOpen ? "none" : "rotate(-90deg)" }} />
                {t("conn.legacy_title")}
              </button>
            </h2>
            <p>{t("conn.legacy_lead")}</p>
          </div>
          {legacyOpen ? (
            <div className="lcard">
              {legacy.map((c) => (
                <ConnectionRow
                  key={c.id}
                  conn={c}
                  onChanged={changed}
                  onReplace={setReplacing}
                  onDisconnect={setDisconnecting}
                />
              ))}
            </div>
          ) : null}
        </section>
      ) : null}

      {adding ? (
        <Dialog
          title={t("conn.add_aria", { kind: t(SECTIONS.find((s) => s.kind === adding)!.title) })}
          onClose={() => setAdding(null)}
          width={480}
        >
          {adding === "inference_api" ? (
            <InferenceForm
              mode="create"
              harnesses={harnesses.data?.items}
              pending={create.pending}
              onSubmit={(credential, label) => void create.run("inference_api", credential, label)}
              onCancel={() => setAdding(null)}
            />
          ) : (
            <ConnectionForm
              kind={adding}
              mode="create"
              pending={create.pending}
              onSubmit={(credential, label) => void create.run(adding, credential, label)}
            />
          )}
        </Dialog>
      ) : null}

      {replacing ? (
        <Dialog
          title={t("conn.replace_aria", { kind: replacing.label })}
          onClose={() => setReplacing(null)}
          width={480}
        >
          {replacing.kind === "inference_api" ? (
            <InferenceForm
              mode="replace"
              initial={replacing.config}
              harnesses={harnesses.data?.items}
              pending={replace.pending}
              onSubmit={(credential) => void replace.run(replacing, credential)}
              onCancel={() => setReplacing(null)}
            />
          ) : (
            <ConnectionForm
              kind={replacing.kind as "modal" | "github"}
              mode="replace"
              pending={replace.pending}
              onSubmit={(credential) => void replace.run(replacing, credential)}
            />
          )}
        </Dialog>
      ) : null}

      {disconnecting ? (
        <Dialog
          title={t("conn.disconnect")}
          onClose={() => setDisconnecting(null)}
          width={420}
          footer={
            <>
              <Button variant="ghost" onClick={() => setDisconnecting(null)}>
                {t("common.cancel")}
              </Button>
              <Button variant="danger" disabled={disconnect.pending} onClick={() => void disconnect.run(disconnecting)}>
                {t("conn.confirm_disconnect")}
              </Button>
            </>
          }
        >
          <p>{disconnecting.label}</p>
          {disconnecting.legacy ? <p className="faint small">{t("conn.legacy_note")}</p> : null}
        </Dialog>
      ) : null}
    </div>
  );
}

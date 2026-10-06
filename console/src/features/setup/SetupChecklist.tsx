import { Link } from "react-router-dom";
import { Pill, type Tone } from "../../components/ui";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useAuth } from "../../state/auth";
import { useConnections } from "../connections/ConnectionsPage";
import { computeSetup, type SetupId, type SetupStatus, type SetupSummary } from "./checklist";

const LABEL: Record<SetupId, I18nKey> = {
  account: "setup.account",
  modal: "kind.modal",
  github: "kind.github",
  opencode_zen: "kind.opencode_zen",
  codex: "kind.codex",
};
const TONE: Record<SetupStatus, Tone> = { ready: "ok", pending: "warn", attention: "err", missing: "dim" };

export function SetupList({ summary }: { summary: SetupSummary }) {
  const { t } = useI18n();
  return (
    <ul className="setup-list">
      {summary.items.map((i) => (
        <li key={i.id} data-testid={`setup-${i.id}`} data-status={i.status}>
          <span className="grow">
            {t(LABEL[i.id])}
            {!i.required ? <span className="faint"> · {t("setup.optional")}</span> : null}
          </span>
          <Pill tone={TONE[i.status]}>{t(`setup.status.${i.status}` as I18nKey)}</Pill>
          {i.id !== "account" && i.status !== "ready" ? (
            <Link to="/connections" className="small">
              {i.status === "missing" ? t("setup.connect") : t("setup.review")}
            </Link>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

export function SetupChecklist() {
  const { t } = useI18n();
  const { me } = useAuth();
  const q = useConnections();
  if (!q.data) return null;
  const summary = computeSetup(me, q.data.items);
  if (summary.complete) return null;
  return (
    <section className="card" aria-labelledby="setup-h">
      <h2 id="setup-h">{t("setup.title")}</h2>
      <p className="muted small">{t("setup.intro")}</p>
      <SetupList summary={summary} />
    </section>
  );
}

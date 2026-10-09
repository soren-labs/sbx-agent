import { Link } from "react-router-dom";
import { Icon, Mark } from "../../components/icons";
import { useI18n } from "../../i18n";
import { useAuth } from "../../state/auth";
import { useDocumentTitle } from "../../state/title";
import { useConnections } from "../connections/ConnectionsPage";
import { NewSession } from "../sessions/NewSession";
import { useSessionList } from "../sessions/SessionsPage";
import type { Session } from "../../api/types";
import type { I18nKey } from "../../i18n/en";
import { computeSetup } from "../setup/checklist";

type T = ReturnType<typeof useI18n>["t"];

function greeting(t: T, email?: string) {
  const hour = new Date().getHours();
  const part = t(
    hour < 5
      ? "home.greeting.late"
      : hour < 12
        ? "home.greeting.morning"
        : hour < 18
          ? "home.greeting.afternoon"
          : "home.greeting.evening",
  );
  const name = email ? email.split("@")[0] : null;
  return name ? `${part}, ${name}` : part;
}

function formatRelativeTime(t: T, dateStr?: string) {
  if (!dateStr) return t("time.now");
  const ms = Date.now() - new Date(dateStr).getTime();
  const mins = Math.max(0, Math.floor(ms / 60_000));
  if (mins < 1) return t("time.now");
  if (mins < 60) return t("time.minutes", { n: mins });
  const hours = Math.floor(mins / 60);
  if (hours < 24) return t("time.hours", { n: hours });
  return t("time.days", { n: Math.floor(hours / 24) });
}

export function HomePage() {
  const { t } = useI18n();
  const { me } = useAuth();
  useDocumentTitle(t("nav.home"));
  const recent = useSessionList({ lifecycle: "open", role: "" }, 4);
  const connections = useConnections();

  // Server-provided health only: the checklist never decides readiness itself.
  const setup = computeSetup(me, connections.data?.items ?? []);
  const steps = setup.items.filter((i) => i.id !== "account");
  const doneCount = steps.filter((i) => i.status === "ready").length;

  return (
    <div className="home-content">
      <div className="home-hero">
        <span className="home-hero-mark">
          <Mark />
        </span>
        <h1>{greeting(t, me?.user.email)}</h1>
        <p>{t("home.question")}</p>
      </div>

      {connections.data && !setup.complete ? (
        <section className="setup-card" aria-label={t("setup.heading")}>
          <div className="setup-card-head">
            <div>
              <h2>{t("setup.heading")}</h2>
              <p>{t("setup.progress", { done: doneCount, total: steps.length })}</p>
            </div>
            <span className="setup-ring" style={{ ["--p" as string]: doneCount / steps.length }}>
              {doneCount}/{steps.length}
            </span>
          </div>
          <div className="setup-steps">
            {steps.map((step) => {
              const done = step.status === "ready";
              return (
                <Link
                  key={step.id}
                  to="/connections"
                  className={`setup-step ${done ? "done" : ""}`}
                  data-testid={`setup-step-${step.id}`}
                  data-status={step.status}
                >
                  <span className="setup-step-check">{done ? <Icon name="check" size={11} /> : null}</span>
                  <span>
                    <strong>{t(`setup.step.${step.id}` as I18nKey)}</strong>
                    <small>
                      {done
                        ? t("setup.step.done")
                        : step.status === "missing"
                          ? t(`setup.step.${step.id}.text` as I18nKey)
                          : t(`setup.status.${step.status}` as I18nKey)}
                    </small>
                  </span>
                  {!done && <Icon name="arrowRight" size={13} />}
                </Link>
              );
            })}
          </div>
        </section>
      ) : null}

      {/* Large Prompt-First Composer */}
      <NewSession />

      {recent.items.length > 0 && (
        <section className="home-recent" aria-label={t("home.recent_title")}>
          <div className="home-recent-head">
            <h2>{t("home.recent_title")}</h2>
            <Link to="/sessions">
              {t("home.view_all")} <Icon name="arrowRight" size={12} />
            </Link>
          </div>
          <div className="home-recent-grid">
            {recent.items.slice(0, 4).map((s: Session) => {
              const active = s.lifecycle === "open" && s.activity === "running";
              const failed = s.activity === "failed";
              const tone = active ? "ok" : failed ? "warn" : "idle";
              const label = t(active ? "home.status.working" : failed ? "home.status.attention" : "home.status.idle");
              return (
                <Link key={s.id} to={`/sessions/${s.id}`} className="recent-card">
                  <span className="recent-card-top">
                    <span className={`hs-badge ${tone}`}>
                      <i /> {label}
                    </span>
                    <small>{formatRelativeTime(t, s.updated_at || s.created_at)}</small>
                  </span>
                  <strong>{s.title || s.id}</strong>
                  <small className="recent-card-repo">
                    <Icon name="branch" size={11} />
                    {s.worktree?.repository || t("home.no_repo")}
                  </small>
                </Link>
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}

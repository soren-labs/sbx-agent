import { Link } from "react-router-dom";
import { useI18n } from "../../i18n";
import { useDocumentTitle } from "../../state/title";
import { SetupChecklist } from "../setup/SetupChecklist";
import { NewSession } from "../sessions/NewSession";
import { SessionRow, useSessionList } from "../sessions/SessionsPage";

export function HomePage() {
  const { t } = useI18n();
  useDocumentTitle("");
  const recent = useSessionList({ lifecycle: "open", role: "" }, 5);
  return (
    <div className="narrow home-page">
      <header className="home-heading">
        <p className="eyebrow">{t("auth.workspace")}</p>
        <h1>{t("home.heading")}</h1>
        <p className="muted">{t("home.subtitle")}</p>
      </header>
      <SetupChecklist />
      <NewSession />
      {recent.items.length ? (
        <section>
          <div className="row section-head">
            <h2 className="grow">{t("home.recent")}</h2>
            <Link to="/sessions">{t("composer.view_all")}</Link>
          </div>
          <ul className="session-list">
            {recent.items.map((s) => (
              <SessionRow key={s.id} s={s} />
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

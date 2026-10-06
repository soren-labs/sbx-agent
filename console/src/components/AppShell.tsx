import { NavLink, Outlet } from "react-router-dom";
import { useI18n } from "../i18n";
import type { I18nKey } from "../i18n/en";
import { useAuth } from "../state/auth";
import { useTheme } from "../theme";
import { Icon } from "./icons";

const NAV: { to: string; key: I18nKey; icon: string; end: boolean }[] = [
  { to: "/", key: "nav.home", icon: "plus", end: true },
  { to: "/sessions", key: "nav.sessions", icon: "list", end: false },
  { to: "/projects", key: "nav.projects", icon: "github", end: false },
  { to: "/connections", key: "nav.connections", icon: "plug", end: false },
  { to: "/settings", key: "nav.settings", icon: "gear", end: false },
];

/** Sidebar on desktop; top bar + bottom nav on mobile. */
export function AppShell() {
  const { t } = useI18n();
  const { me, logout } = useAuth();
  useTheme(); // applies data-theme on <html>
  const links = (size: number) =>
    NAV.map((n) => (
      <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => (isActive ? "active" : "")}>
        <Icon name={n.icon} size={size} />
        {t(n.key)}
      </NavLink>
    ));
  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        {t("common.skip")}
      </a>
      <nav className="side-nav" aria-label={t("nav.primary")}>
        <div className="brand">{t("app.name")}</div>
        {links(17)}
        <div className="grow" />
        <div className="faint small account">{me?.user.email}</div>
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => void logout()}>
          {t("auth.logout")}
        </button>
      </nav>
      <div className="grow" style={{ display: "flex", flexDirection: "column", minHeight: "100dvh", minWidth: 0 }}>
        <header className="top-bar">
          <span className="brand">{t("app.name")}</span>
          <span className="grow" />
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => void logout()}>
            {t("auth.logout")}
          </button>
        </header>
        <main className="main" id="main" tabIndex={-1}>
          <div className="main-inner">
            <Outlet />
          </div>
        </main>
      </div>
      <nav className="bottom-nav" aria-label={t("nav.primary_mobile")}>
        {links(20)}
      </nav>
    </div>
  );
}

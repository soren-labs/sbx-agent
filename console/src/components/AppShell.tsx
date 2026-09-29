import { NavLink, Outlet } from "react-router-dom";
import { useI18n } from "../i18n";
import { useTheme } from "../theme";
import { Icon } from "./icons";

const NAV = [
  { to: "/", key: "nav.new", icon: "plus", end: true },
  { to: "/sessions", key: "nav.sessions", icon: "list", end: false },
  { to: "/integrations", key: "nav.integrations", icon: "plug", end: false },
  { to: "/settings", key: "nav.settings", icon: "gear", end: false },
] as const;

/**
 * App shell — the whole product IA:
 *   New Session / Sessions / Integrations / Settings.
 * Sidebar on desktop, top bar + bottom nav on mobile.
 */
export function AppShell() {
  const { t } = useI18n();
  useTheme(); // applies data-theme on <html>
  return (
    <div className="shell">
      <a className="skip-link" href="#main">{t("common.skip")}</a>
      <nav className="side-nav" aria-label="primary">
        <div className="brand">{t("app.name")}</div>
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.end}
            className={({ isActive }) => (isActive ? "active" : "")}
          >
            <Icon name={n.icon} />
            {t(n.key)}
          </NavLink>
        ))}
      </nav>
      <div className="grow" style={{ display: "flex", flexDirection: "column", minHeight: "100dvh" }}>
        <header className="top-bar">
          <span className="brand">{t("app.name")}</span>
        </header>
        <main className="main" id="main">
          <div className="main-inner">
            <Outlet />
          </div>
        </main>
      </div>
      <nav className="bottom-nav" aria-label="primary mobile">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.end}
            className={({ isActive }) => (isActive ? "active" : "")}
          >
            <Icon name={n.icon} size={20} />
            {t(n.key)}
          </NavLink>
        ))}
      </nav>
    </div>
  );
}

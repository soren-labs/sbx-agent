import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import type { Session } from "../api/types";
import { Icon, Mark } from "./icons";
import { useI18n } from "../i18n";
import { useAuth } from "../state/auth";
import { useApi } from "../state/context";
import { useQuery } from "../state/query";
import { useTheme } from "../theme";
import { computeSetup } from "../features/setup/checklist";

type T = ReturnType<typeof useI18n>["t"];

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

const shortcutModifier = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl";

/** Server activity values: idle, queued, running, awaiting_input, attention. */
function isSessionActive(s: Session) {
  return s.lifecycle === "open" && (s.activity === "running" || s.activity === "queued");
}

function sessionSubtitle(t: T, s: Session) {
  const time = formatRelativeTime(t, s.updated_at || s.created_at);
  if (isSessionActive(s)) return t("shell.status.working", { time });
  if (s.lifecycle === "closed") return t("shell.status.closed", { time });
  if (s.activity === "attention") return t("shell.status.attention", { time });
  if (s.activity === "awaiting_input") return t("shell.status.waiting", { time });
  return t("shell.status.idle", { time });
}

export function AppShell() {
  const { t } = useI18n();
  const { me, workspace, logout } = useAuth();
  useTheme();
  const location = useLocation();
  const navigate = useNavigate();
  const api = useApi();
  const w = workspace?.id ?? null;

  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [search, setSearch] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);

  const [pinned, setPinned] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem("sbx.console.pinned") ?? "[]");
    } catch {
      return [];
    }
  });

  const sessionsQuery = useQuery(
    w ? ["sessions", w] : null,
    () => api.sessions.list(w!),
    { pollMs: 6000 },
  );

  const connectionsQuery = useQuery(
    w ? ["connections", w] : null,
    () => api.connections.list(w!),
  );

  const allSessions = sessionsQuery.data?.items ?? [];
  // Same server-health projection as the Home checklist; never a count of rows.
  const connectionsIncomplete =
    connectionsQuery.data !== undefined && !computeSetup(me, connectionsQuery.data.items).complete;

  const togglePin = useCallback((id: string) => {
    setPinned((prev) => {
      const next = prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id];
      localStorage.setItem("sbx.console.pinned", JSON.stringify(next));
      return next;
    });
  }, []);

  useLayoutEffect(() => {
    window.scrollTo(0, 0);
  }, [location.pathname]);

  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCollapsed(false);
        setSidebarOpen(true);
        searchRef.current?.focus();
      }
      if ((e.metaKey || e.ctrlKey) && (e.key === "0" || e.key.toLowerCase() === "o")) {
        e.preventDefault();
        navigate("/");
      }
      if (e.key === "Escape") {
        setSidebarOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate]);

  const titleOf = (s: Session) => s.title || t("session.untitled");
  const filteredSessions = allSessions.filter((s) => titleOf(s).toLowerCase().includes(search.toLowerCase()));

  const pinnedSessions = filteredSessions.filter((s) => pinned.includes(s.id));
  const recentSessions = filteredSessions.filter((s) => !pinned.includes(s.id));
  const username = me?.user.email.split("@")[0] ?? "developer";
  const initials = (me?.user.email[0] ?? "U").toUpperCase();

  const currentSection = t(
    location.pathname.startsWith("/connections")
      ? "nav.connections"
      : location.pathname.startsWith("/settings")
        ? "nav.settings"
        : location.pathname.startsWith("/projects")
          ? "nav.projects"
          : location.pathname.startsWith("/sessions")
            ? "nav.sessions"
            : "nav.home",
  );

  const renderSessionRow = (s: Session) => {
    const active = isSessionActive(s);
    const failed = s.activity === "attention";
    const isPinned = pinned.includes(s.id);

    return (
      <div className="sidebar-session-row" key={s.id}>
        <NavLink
          to={`/sessions/${s.id}`}
          className={({ isActive }) => `sidebar-session ${isActive ? "selected" : ""}`}
        >
          <span className={`sidebar-session-state ${active ? "live" : failed ? "attention" : ""}`}>
            <Icon name={active ? "clock" : failed ? "warn" : "sessions"} size={13} />
          </span>
          <span className="sidebar-session-text">
            <span className="sidebar-session-title">{titleOf(s)}</span>
            <small>{sessionSubtitle(t, s)}</small>
          </span>
          {active && <span className="unread-dot" />}
        </NavLink>
        <button
          type="button"
          className="row-pin icon-button"
          aria-label={t(isPinned ? "shell.unpin" : "shell.pin", { title: titleOf(s) })}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            togglePin(s.id);
          }}
        >
          <Icon name="pin" size={11} />
        </button>
      </div>
    );
  };

  return (
    <div className={`prototype app-shell-root ${collapsed ? "sidebar-collapsed" : ""}`}>
      <a className="skip-link" href="#workspace-main">
        {t("common.skip")}
      </a>
      {sidebarOpen && (
        <button
          type="button"
          className="sidebar-scrim"
          aria-label={t("shell.close_nav")}
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Persistent Left Sidebar matching image(9) and image(10) */}
      <nav className={`workspace-sidebar side-nav ${sidebarOpen ? "mobile-open" : ""}`} aria-label={t("nav.primary")}>
        <div className="sidebar-org-row">
          <Link className="workspace-brand" to="/">
            <Mark small />
            <span>SBX Agent</span>
          </Link>
          <button
            type="button"
            className="icon-button desktop-toggle"
            aria-label={t(collapsed ? "shell.expand" : "shell.collapse")}
            onClick={() => setCollapsed(!collapsed)}
          >
            <Icon name="menu" size={15} />
          </button>
        </div>

        <Link to="/" className="new-session-button">
          <Icon name="plus" size={15} />
          <span>{t("shell.new_session")}</span>
          <kbd>{shortcutModifier} 0</kbd>
        </Link>

        <label className="sidebar-search">
          <Icon name="search" size={13} />
          <input
            ref={searchRef}
            aria-label={t("shell.search")}
            placeholder={t("shell.search_ph")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <kbd>{shortcutModifier} K</kbd>
        </label>

        <NavLink to="/sessions" end className={({ isActive }) => `sessions-nav ${isActive ? "active" : ""}`}>
          <Icon name="sessions" size={14} />
          <span>{t("nav.sessions")}</span>
          <span className="sidebar-nav-badge" aria-hidden="true">
            {allSessions.length}
          </span>
        </NavLink>
        <NavLink to="/projects" className={({ isActive }) => `sessions-nav ${isActive ? "active" : ""}`}>
          <Icon name="branch" size={14} />
          <span>{t("nav.projects")}</span>
        </NavLink>

        <div className="sidebar-session-scroll">
          {pinnedSessions.length > 0 && (
            <>
              <div className="sidebar-section-label">
                <Icon name="pin" size={11} />
                <span>{t("shell.pinned")}</span>
                <span>{pinnedSessions.length}</span>
              </div>
              {pinnedSessions.map(renderSessionRow)}
            </>
          )}

          <div className="sidebar-section-label">
            <span>{t("shell.recent")}</span>
            <span>{recentSessions.length}</span>
          </div>

          {recentSessions.map(renderSessionRow)}

          {filteredSessions.length === 0 && (
            <p className="sidebar-empty">
              {t(sessionsQuery.loading ? "shell.loading" : "shell.empty")}
            </p>
          )}
        </div>

        <div className="sidebar-bottom">
          <NavLink
            to="/connections"
            className={({ isActive }) => `sidebar-bottom-link ${isActive ? "active" : ""}`}
          >
            <Icon name="plug" size={14} />
            <span>{t("nav.connections")}</span>
            {connectionsIncomplete && <span className="integration-alert" />}
          </NavLink>

          <NavLink
            to="/settings"
            className={({ isActive }) => `sidebar-bottom-link ${isActive ? "active" : ""}`}
          >
            <Icon name="settings" size={14} />
            <span>{t("nav.settings")}</span>
          </NavLink>

          <div className="sidebar-user">
            <span className="user-avatar avatar">{initials}</span>
            <div className="sidebar-user-details">
              <strong>{username}</strong>
              <small className="sidebar-user-status">
                <i />
                {workspace?.name || "Personal workspace"}
              </small>
            </div>
            <button
              type="button"
              className="icon-button"
              aria-label={t("auth.logout")}
              title={t("auth.logout")}
              onClick={() => void logout()}
            >
              <Icon name="logout" size={14} />
            </button>
          </div>
        </div>
      </nav>

      {/* Main Workspace Body */}
      <div className="workspace-body">
        <header className="workspace-topbar">
          <button
            type="button"
            className="icon-button mobile-toggle"
            aria-label={t("shell.open_nav")}
            onClick={() => setSidebarOpen(true)}
          >
            <Icon name="menu" size={17} />
          </button>
          <span className="topbar-crumb">{currentSection}</span>
        </header>

        <main id="workspace-main" tabIndex={-1}>
          <Outlet />
        </main>
      </div>

      {/* Mobile Bottom Navigation matching requirement */}
      <nav className="bottom-nav" aria-label={t("nav.primary_mobile")}>
        <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
          <Icon name="plus" size={18} />
          <span>{t("nav.new")}</span>
        </NavLink>
        <NavLink to="/sessions" className={({ isActive }) => (isActive ? "active" : "")}>
          <Icon name="list" size={18} />
          <span>{t("nav.sessions")}</span>
        </NavLink>
        <NavLink to="/connections" className={({ isActive }) => (isActive ? "active" : "")}>
          <Icon name="plug" size={18} />
          <span>{t("nav.connections")}</span>
        </NavLink>
        <NavLink to="/settings" className={({ isActive }) => (isActive ? "active" : "")}>
          <Icon name="settings" size={18} />
          <span>{t("nav.settings")}</span>
        </NavLink>
      </nav>
    </div>
  );
}

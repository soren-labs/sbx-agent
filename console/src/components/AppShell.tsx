import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import type { Session } from "../api/types";
import { Icon, Mark } from "./icons";
import { useI18n } from "../i18n";
import { useAuth } from "../state/auth";
import { useApi } from "../state/context";
import { useQuery } from "../state/query";
import { useTheme } from "../theme";

function formatRelativeTime(dateStr?: string) {
  if (!dateStr) return "Just now";
  const ms = Date.now() - new Date(dateStr).getTime();
  const mins = Math.max(0, Math.floor(ms / 60_000));
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

const shortcutModifier = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl";

function isSessionActive(s: Session) {
  return s.lifecycle === "open" && (s.activity === "running" || s.activity === "starting" || s.activity === "queued");
}

function sessionSubtitle(s: Session) {
  const time = formatRelativeTime(s.updated_at || s.created_at);
  if (isSessionActive(s)) return `Working · ${time}`;
  if (s.lifecycle === "closed") return `Closed · ${time}`;
  if (s.activity === "failed") return `Needs attention · ${time}`;
  if (s.title.toLowerCase().includes("pr") || s.labels?.includes("pr")) return `PR ready · ${time}`;
  return `Idle · ${time}`;
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
  const connections = connectionsQuery.data?.items ?? [];
  const connectionsIncomplete = connections.length < 3 || connections.some((c) => c.health !== "ready");

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

  const filteredSessions = allSessions.filter((s) =>
    s.title.toLowerCase().includes(search.toLowerCase()),
  );

  const pinnedSessions = filteredSessions.filter((s) => pinned.includes(s.id));
  const recentSessions = filteredSessions.filter((s) => !pinned.includes(s.id));
  const reviewSessions = allSessions.filter(
    (s) => s.title.toLowerCase().includes("pr") || s.labels?.includes("pr"),
  );

  const username = me?.user.email.split("@")[0] ?? "developer";
  const initials = (me?.user.email[0] ?? "U").toUpperCase();

  const currentSection = location.pathname.startsWith("/connections")
    ? "Connections"
    : location.pathname.startsWith("/settings")
      ? "Settings"
      : location.pathname.startsWith("/projects")
        ? "Projects"
        : location.pathname.startsWith("/sessions")
          ? "Sessions"
          : "Home";

  const renderSessionRow = (s: Session) => {
    const active = isSessionActive(s);
    const failed = s.activity === "failed";
    const isPr = s.title.toLowerCase().includes("pr") || s.labels?.includes("pr");
    const isPinned = pinned.includes(s.id);

    return (
      <div className="sidebar-session-row" key={s.id}>
        <NavLink
          to={`/sessions/${s.id}`}
          className={({ isActive }) => `sidebar-session ${isActive ? "selected" : ""}`}
        >
          <span className={`sidebar-session-state ${active ? "live" : failed ? "attention" : ""}`}>
            <Icon name={active ? "clock" : failed ? "x" : isPr ? "pr" : "clock"} size={13} />
          </span>
          <span className="sidebar-session-text">
            <span className="sidebar-session-title">{s.title}</span>
            <small>{sessionSubtitle(s)}</small>
          </span>
          {active && <span className="unread-dot" />}
        </NavLink>
        <button
          type="button"
          className="row-pin icon-button"
          aria-label={isPinned ? `Unpin ${s.title}` : `Pin ${s.title}`}
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
          aria-label="Close navigation"
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
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            onClick={() => setCollapsed(!collapsed)}
          >
            <Icon name="menu" size={15} />
          </button>
        </div>

        <Link to="/" className="new-session-button">
          <Icon name="plus" size={15} />
          <span>New session</span>
          <kbd>{shortcutModifier} 0</kbd>
        </Link>

        <label className="sidebar-search">
          <Icon name="search" size={13} />
          <input
            ref={searchRef}
            aria-label="Search sessions"
            placeholder="Search sessions…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <kbd>{shortcutModifier} K</kbd>
        </label>

        <NavLink to="/sessions" className={({ isActive }) => `sessions-nav ${isActive ? "active" : ""}`}>
          <Icon name="pr" size={14} />
          <span>Review</span>
          <span className="sidebar-nav-badge">{reviewSessions.length}</span>
        </NavLink>

        <div className="sidebar-section-label sessions-section-title">
          <NavLink to="/sessions" end className={({ isActive }) => (isActive ? "active" : "")}>
            Sessions
          </NavLink>
          <Link to="/" aria-label="New session">
            <Icon name="plus" size={13} />
          </Link>
        </div>

        <div className="sidebar-session-scroll">
          {pinnedSessions.length > 0 && (
            <>
              <div className="sidebar-section-label">
                <Icon name="pin" size={11} />
                <span>Pinned</span>
                <span>{pinnedSessions.length}</span>
              </div>
              {pinnedSessions.map(renderSessionRow)}
            </>
          )}

          <div className="sidebar-section-label">
            <span>Recent</span>
            <span>{recentSessions.length}</span>
          </div>

          {recentSessions.map(renderSessionRow)}

          {filteredSessions.length === 0 && (
            <p className="sidebar-empty">
              {sessionsQuery.loading ? "Loading sessions…" : "No sessions found."}
            </p>
          )}
        </div>

        <div className="sidebar-bottom">
          <NavLink
            to="/connections"
            className={({ isActive }) => `sidebar-bottom-link ${isActive ? "active" : ""}`}
          >
            <Icon name="plug" size={14} />
            <span>Connections</span>
            {connectionsIncomplete && <span className="integration-alert" />}
          </NavLink>

          <NavLink
            to="/settings"
            className={({ isActive }) => `sidebar-bottom-link ${isActive ? "active" : ""}`}
          >
            <Icon name="settings" size={14} />
            <span>Settings</span>
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
            aria-label="Open navigation"
            onClick={() => setSidebarOpen(true)}
          >
            <Icon name="menu" size={17} />
          </button>
          <span className="topbar-crumb">{currentSection}</span>
          <div className="topbar-right">
            <span className="topbar-status">
              <span />
              Prototype · local data
            </span>
          </div>
        </header>

        <main id="workspace-main" tabIndex={-1}>
          <Outlet />
        </main>
      </div>

      {/* Mobile Bottom Navigation matching requirement */}
      <nav className="bottom-nav" aria-label={t("nav.primary_mobile")}>
        <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
          <Icon name="plus" size={18} />
          <span>New Session</span>
        </NavLink>
        <NavLink to="/sessions" className={({ isActive }) => (isActive ? "active" : "")}>
          <Icon name="list" size={18} />
          <span>Sessions</span>
        </NavLink>
        <NavLink to="/connections" className={({ isActive }) => (isActive ? "active" : "")}>
          <Icon name="plug" size={18} />
          <span>Connections</span>
        </NavLink>
        <NavLink to="/settings" className={({ isActive }) => (isActive ? "active" : "")}>
          <Icon name="settings" size={18} />
          <span>Settings</span>
        </NavLink>
      </nav>
    </div>
  );
}

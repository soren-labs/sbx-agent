import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import {
  ChevronDown,
  CircleHelp,
  Clock,
  FolderGit2,
  Menu as MenuIcon,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Plug,
  Search,
  Server,
  Settings,
  SlidersHorizontal,
} from "lucide-react";
import type { Session } from "../api/types";
import { useI18n } from "../i18n";
import type { I18nKey } from "../i18n/en";
import { useAuth } from "../state/auth";
import { useApi } from "../state/context";
import { useQuery } from "../state/query";
import { useTheme } from "../theme";
import { IconButton } from "../ui/Button";
import { Menu, MenuItem } from "../ui/Menu";
import { Skeleton } from "../ui/Skeleton";
import { StatusDot } from "../ui/Pill";
import { formatRelative } from "../ui/format";
import { useDebounced } from "../ui/useDebounced";
import { harnessName } from "../features/sessions/harnesses";
import { activityLabelKey, activityTone } from "../features/sessions/activity";
import { UserMenu } from "./UserMenu";

const COLLAPSED_KEY = "sbx.console.sidebar";
const DOCS_URL = "https://docs.sbx-agent.com";

// Statuses worth a label in the subtitle; quiet states fall back to harness · model · time.
const LABELLED_STATUS: ReadonlySet<I18nKey> = new Set(["status.running", "status.queued", "status.waiting", "status.attention"]);

function sessionSubtitle(s: Session, locale: string, t: (k: I18nKey) => string): string {
  const time = formatRelative(s.updated_at ?? s.created_at, locale);
  const labelKey = activityLabelKey(s);
  if (LABELLED_STATUS.has(labelKey)) return `${t(labelKey)} · ${time}`;
  const model = s.harness.model;
  return `${harnessName(s.harness.provider_id)} · ${model ?? "server default"} · ${time}`;
}

function SessionItem({ s, locale, tag }: { s: Session; locale: string; tag?: string | null }) {
  const { t } = useI18n();
  const tone = activityTone(s);
  return (
    <NavLink to={`/sessions/${s.id}`} className={({ isActive }) => `session-item${isActive ? " session-item--active" : ""}`}>
      <span className="session-item__title truncate">{s.title || t("sessions.untitled")}</span>
      <span className="session-item__meta truncate">{sessionSubtitle(s, locale, t)}</span>
      {tag ? <span className="session-item__tag">{tag}</span> : tone ? <StatusDot tone={tone} className="session-item__dot" /> : null}
    </NavLink>
  );
}

export function AppShell() {
  const { t, locale } = useI18n();
  const { me, workspace } = useAuth();
  useTheme();
  const api = useApi();
  const location = useLocation();
  const w = workspace?.id ?? null;

  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(COLLAPSED_KEY) === "1");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [filterOpen, setFilterOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [showClosed, setShowClosed] = useState(false);
  const filterRef = useRef<HTMLInputElement>(null);

  const toggleCollapsed = () => {
    setCollapsed((c) => {
      localStorage.setItem(COLLAPSED_KEY, c ? "0" : "1");
      return !c;
    });
  };

  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Archived and closed Sessions are listed only when asked for; search runs on the server
  // over the whole workspace, not over the recent page already loaded.
  const lifecycles = ["open", ...(showArchived ? ["archived"] : []), ...(showClosed ? ["closed"] : [])].join(",");
  const needle = useDebounced(filter.trim(), 250);
  const sessionsQuery = useQuery(
    w ? ["sessions", w, "nav", lifecycles, needle] : null,
    () => api.sessions.list(w!, { lifecycle: lifecycles, limit: 50, ...(needle ? { q: needle } : {}) }),
    { pollMs: 15000 },
  );

  const all: Session[] = sessionsQuery.data?.items ?? [];
  const items = all;
  const more = Boolean(sessionsQuery.data?.next_cursor);

  const openFilter = () => {
    setFilterOpen(true);
    requestAnimationFrame(() => filterRef.current?.focus());
  };

  const workspaceInitial = (workspace?.name ?? me?.user.email ?? "?")[0].toUpperCase();
  const emailInitial = (me?.user.email ?? "?")[0].toUpperCase();

  const sidebar = (
    <aside className={`sidebar${drawerOpen ? " sidebar--open" : ""}`} aria-label={t("shell.workspace")}>
      <div className="sidebar__workspace">
        <UserMenu
          trigger={
            <button type="button" className="sidebar__workspace-btn">
              <span className="sidebar__avatar">{workspaceInitial}</span>
              <span className="sidebar__workspace-name truncate">{workspace?.name ?? t("shell.workspace")}</span>
              <ChevronDown size={14} className="sidebar__chevron" />
            </button>
          }
        />
        <div className="sidebar__workspace-actions">
          <IconButton label={t("shell.search")} icon={<Search size={15} />} onClick={openFilter} />
          <IconButton label={t("shell.collapse")} icon={<PanelLeftClose size={15} />} onClick={toggleCollapsed} className="sidebar__collapse-btn" />
        </div>
      </div>

      <nav className="sidebar__nav" aria-label="Primary">
        <NavLink to="/" end className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`}>
          <Plus size={16} />
          <span>{t("nav.new")}</span>
        </NavLink>
        <NavLink to="/projects" className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`}>
          <FolderGit2 size={16} />
          <span>{t("nav.projects")}</span>
        </NavLink>
        <NavLink to="/machines" className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`}>
          <Server size={16} />
          <span>{t("nav.machines")}</span>
        </NavLink>
        <NavLink to="/connections" className={({ isActive }) => `nav-item${isActive ? " nav-item--active" : ""}`}>
          <Plug size={16} />
          <span>{t("nav.connections")}</span>
        </NavLink>
      </nav>

      <div className="sidebar__sessions">
        <div className="sidebar__sessions-head">
          <NavLink to="/sessions" className="sidebar__section-link">
            {t("nav.sessions")}
          </NavLink>
          <div className="sidebar__sessions-actions">
            <Link to="/" className="icon-btn icon-btn--sm" aria-label={t("nav.new")} title={t("nav.new")}>
              <Plus size={14} />
            </Link>
            <IconButton
              size="sm"
              label={t("shell.search")}
              icon={<SlidersHorizontal size={13} />}
              onClick={() => (filterOpen ? setFilterOpen(false) : openFilter())}
            />
            <Menu
              trigger={<IconButton size="sm" label={t("shell.options")} icon={<MoreHorizontal size={14} />} />}
              align="end"
              label={t("shell.options")}
            >
              <MenuItem label={t("shell.show_archived")} checked={showArchived} onSelect={() => setShowArchived((v) => !v)} />
              <MenuItem label={t("shell.show_closed")} checked={showClosed} onSelect={() => setShowClosed((v) => !v)} />
            </Menu>
          </div>
        </div>

        {filterOpen ? (
          <input
            ref={filterRef}
            className="sidebar__filter input"
            placeholder={t("shell.search_ph")}
            aria-label={t("shell.search_ph")}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        ) : null}

        <div className="sidebar__group">
          <Clock size={11} />
          <span>{t("shell.recent")}</span>
        </div>

        <div className="sidebar__list">
          {sessionsQuery.loading && !all.length ? (
            <>
              {Array.from({ length: 5 }, (_, i) => (
                <div key={i} className="session-item session-item--skeleton">
                  <Skeleton width="70%" height={12} />
                  <Skeleton width="45%" height={10} />
                </div>
              ))}
            </>
          ) : items.length ? (
            items.map((s) => (
              <SessionItem
                key={s.id}
                s={s}
                locale={locale}
                tag={
                  // Lifecycle tag only when the user opted those Sessions into the list.
                  s.lifecycle === "closed" && showClosed
                    ? t("status.closed")
                    : s.lifecycle === "archived" && showArchived
                      ? t("status.archived")
                      : null
                }
              />
            ))
          ) : (
            <p className="sidebar__empty">{needle ? t("shell.search_none") : t("shell.sessions_empty")}</p>
          )}
          {more ? (
            <Link className="sidebar__more" to={needle ? `/sessions?q=${encodeURIComponent(needle)}` : "/sessions"}>
              {t("shell.all_sessions")}
            </Link>
          ) : null}
        </div>
      </div>

      <div className="sidebar__footer">
        <a className="icon-btn" href={DOCS_URL} target="_blank" rel="noopener noreferrer" aria-label={t("shell.help")} title={t("shell.help")}>
          <CircleHelp size={15} />
        </a>
        <Link to="/settings" className="icon-btn" aria-label={t("shell.settings")} title={t("shell.settings")}>
          <Settings size={15} />
        </Link>
        <span className="sidebar__footer-spacer" />
        <UserMenu
          side="top"
          align="end"
          trigger={
            <button type="button" className="sidebar__avatar sidebar__avatar--user" aria-label={t("shell.account")}>
              {emailInitial}
            </button>
          }
        />
      </div>
    </aside>
  );

  return (
    <div className={`shell${collapsed ? " shell--collapsed" : ""}${drawerOpen ? " shell--drawer" : ""}`}>
      <a className="skip-link" href="#main">
        {t("common.skip")}
      </a>
      {drawerOpen ? <button type="button" className="shell__scrim" aria-label={t("shell.close_nav")} onClick={() => setDrawerOpen(false)} /> : null}
      {sidebar}
      <div className="shell__main">
        <header className="topbar">
          <IconButton
            label={t("shell.expand")}
            icon={<PanelLeftOpen size={16} />}
            onClick={toggleCollapsed}
            className="topbar__expand"
          />
          <IconButton
            label={t("shell.open_nav")}
            icon={<MenuIcon size={17} />}
            onClick={() => setDrawerOpen(true)}
            className="topbar__hamburger"
          />
        </header>
        <main id="main" className="shell__content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}

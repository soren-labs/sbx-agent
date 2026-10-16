import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Activity, FolderTree, GitCompare, GitFork, Globe, LayoutDashboard, SquareTerminal } from "lucide-react";
import type { Session } from "../../api/types";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useApi } from "../../state/context";
import { useQuery } from "../../state/query";
import type { SessionLive, SessionLiveState } from "../../state/session-live";
import { ActivityTab } from "./tabs/ActivityTab";
import { ChangesTab } from "./tabs/ChangesTab";
import { ChildrenTab } from "./tabs/ChildrenTab";
import { FilesTab } from "./tabs/FilesTab";
import { OverviewTab } from "./tabs/OverviewTab";
import { ServicesTab } from "./tabs/ServicesTab";
import { TerminalTab } from "./tabs/TerminalTab";

/** Workspace panels beside the conversation. "conversation" is the bare Session route. */
export const PANELS = ["overview", "changes", "files", "terminal", "services", "children", "activity"] as const;
export type Panel = (typeof PANELS)[number];

const ICONS: Record<Panel, ReactNode> = {
  overview: <LayoutDashboard size={14} />,
  changes: <GitCompare size={14} />,
  files: <FolderTree size={14} />,
  terminal: <SquareTerminal size={14} />,
  services: <Globe size={14} />,
  children: <GitFork size={14} />,
  activity: <Activity size={14} />,
};



export function WorkspacePanel({
  session,
  live,
  state,
  panel,
  onSelect,
}: {
  session: Session;
  live: SessionLive;
  state: SessionLiveState;
  panel: Panel;
  onSelect: (p: Panel) => void;
}) {
  const { t } = useI18n();
  const api = useApi();
  // Badges share the same query keys as their tabs: one fetch serves both.
  const rev = state.revisions;
  const liveChanges = useQuery(["changes-live", session.id, rev.changes + rev.deliveries], () => api.changes.live(session.id));
  const sets = useQuery(["changesets", session.id, rev.changes + rev.deliveries], () => api.changes.list(session.id));
  const services = useQuery(["services", session.id, rev.services], () => api.services.list(session.id));
  const delegations = useQuery(["delegations", session.id, rev.delegations + rev.changes], () => api.delegations.list(session.id));
  const changeCount =
    liveChanges.data?.observation.files.length ?? sets.data?.items[0]?.file_count ?? undefined;
  const runningServices = (services.data?.items ?? []).filter((s) => s.state === "running" || s.state === "ready").length;
  const childCount = delegations.data?.items.length;
  const badges: Partial<Record<Panel, number>> = {
    changes: changeCount,
    services: runningServices || undefined,
    children: childCount,
  };

  const tabsRef = useRef<HTMLElement>(null);
  const [iconOnly, setIconOnly] = useState(false);
  // Natural width of the fully-labelled tab row, sampled only while labels are
  // visible (icon-only shrinks scrollWidth and would make it look like it fits).
  const naturalRef = useRef(0);
  useLayoutEffect(() => {
    const el = tabsRef.current;
    if (el && !iconOnly) naturalRef.current = el.scrollWidth;
  });
  useEffect(() => {
    const el = tabsRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([entry]) => {
      if (!el.classList.contains("wpanel__tabs--icons")) naturalRef.current = el.scrollWidth;
      setIconOnly(entry.contentRect.width < naturalRef.current);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <>
      <nav ref={tabsRef} className={`wpanel__tabs${iconOnly ? " wpanel__tabs--icons" : ""}`} aria-label={t("session.sections")} role="tablist">
        {PANELS.map((x) => {
          const label = t(`tab.${x}` as I18nKey);
          return (
            <button
              key={x}
              type="button"
              role="tab"
              className={`wpanel__tab${panel === x ? " wpanel__tab--active" : ""}`}
              aria-selected={panel === x}
              aria-label={label}
              title={label}
              onClick={() => onSelect(x)}
            >
              {ICONS[x]}
              <span className="wpanel__tab-label">{label}</span>
              {badges[x] != null && badges[x]! > 0 ? <span className="wpanel__badge">{badges[x]}</span> : null}
            </button>
          );
        })}
      </nav>
      <div className="wpanel__body">
        {panel === "overview" && <OverviewTab session={session} state={state} live={live} />}
        {panel === "activity" && <ActivityTab sessionId={session.id} state={state} />}
        {panel === "changes" && <ChangesTab sessionId={session.id} revision={rev.changes + rev.deliveries} />}
        {panel === "files" && <FilesTab session={session} live={live} rev={rev.changes} />}
        {panel === "terminal" && <TerminalTab sessionId={session.id} sessionClosed={session.lifecycle === "closed"} />}
        {panel === "services" && <ServicesTab sessionId={session.id} revision={rev.services} />}
        {panel === "children" && <ChildrenTab sessionId={session.id} revision={rev.delegations + rev.changes} />}
      </div>
    </>
  );
}

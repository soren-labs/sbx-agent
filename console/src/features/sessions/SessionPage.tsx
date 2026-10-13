import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Github, MoreHorizontal, PanelRight } from "lucide-react";
import type { Session } from "../../api/types";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useApi, useQueryClient } from "../../state/context";
import { useSessionLive } from "../../state/use-session-live";
import { useDocumentTitle } from "../../state/title";
import { IconButton } from "../../ui/Button";
import { EmptyState } from "../../ui/EmptyState";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Menu, MenuItem, MenuSeparator } from "../../ui/Menu";
import { Pill, type Tone } from "../../ui/Pill";
import { Skeleton } from "../../ui/Skeleton";
import { useToast } from "../../ui/Toast";
import { useAction } from "../../ui/useAction";
import { ConversationPane } from "./ConversationPane";
import { harnessName } from "./harnesses";
import { isLiveTurn } from "./worklog";
import { PANELS, WorkspacePanel, type Panel } from "./WorkspacePanel";

const PANEL_KEY = "sbx.console.workspacePanel";
const SPLIT_KEY = "sbx.console.split";
const MIN_PANEL = 380;
const MAX_FRAC = 0.7;
const DEFAULT_FRAC = 0.46;

function loadSplit(): number {
  const v = Number(localStorage.getItem(SPLIT_KEY));
  return Number.isFinite(v) && v > 0 && v <= MAX_FRAC ? v : DEFAULT_FRAC;
}

/** Status from old `statusLabel` semantics: the live Turn is the most precise truth. */
function statusOf(s: Session, liveTurn: { state: string } | undefined, t: (k: I18nKey) => string): { label: string; tone: Tone } {
  if (s.lifecycle === "closed") return { label: t("session.status.closed"), tone: "dim" };
  if (liveTurn) {
    return {
      label: t(liveTurn.state === "running" ? "activity.running" : liveTurn.state === "queued" ? "activity.queued" : "turn.preparing_short"),
      tone: "run",
    };
  }
  const key = `activity.${s.activity}` as I18nKey;
  const label = t(key) === key ? s.activity.replaceAll("_", " ") : t(key);
  const a = s.activity;
  const tone: Tone = a === "attention" || /fail|error/.test(a) ? "err" : /await|wait/.test(a) ? "warn" : "dim";
  return { label, tone };
}

export function SessionPage() {
  const { t } = useI18n();
  const api = useApi();
  const nav = useNavigate();
  const toast = useToast();
  const params = useParams();
  const id = params.id!;
  const routed = params.tab ?? "conversation";
  const panel: Panel = (PANELS as readonly string[]).includes(routed) ? (routed as Panel) : "overview";
  const showPanel = routed !== "conversation";
  const { live, state } = useSessionLive(id);
  const s = state.session;
  useDocumentTitle(s?.title || t("session.untitled"));
  // The sidebar lists this Session too: it should not keep saying "Working" until its next poll.
  const qc = useQueryClient();
  const activity = s?.activity;
  const workspace = s?.workspace_id;
  useEffect(() => {
    if (workspace && activity) qc.invalidate(["sessions", workspace]);
  }, [qc, workspace, activity]);

  const lifecycle = useAction(async (key, kind: "archive" | "unarchive" | "close") => {
    await api.sessions[kind](id, { idempotencyKey: key });
    live.refresh(["session"]);
  });

  const [panelOpen, setPanelOpen] = useState(() => localStorage.getItem(PANEL_KEY) !== "0");
  const togglePanel = () =>
    setPanelOpen((v) => {
      localStorage.setItem(PANEL_KEY, v ? "0" : "1");
      return !v;
    });
  const [split, setSplit] = useState(loadSplit);
  const splitNow = useRef(split);
  splitNow.current = split;
  const onDividerDown = useCallback((down: React.PointerEvent) => {
    down.preventDefault();
    const grid = (down.target as HTMLElement).closest<HTMLElement>(".wb-grid");
    if (!grid) return;
    const rect = grid.getBoundingClientRect();
    const move = (clientX: number) => {
      const frac = (rect.right - clientX) / rect.width;
      setSplit(Math.min(MAX_FRAC, Math.max(MIN_PANEL / rect.width, frac)));
    };
    move(down.clientX);
    const onMove = (e: PointerEvent) => move(e.clientX);
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      localStorage.setItem(SPLIT_KEY, String(splitNow.current));
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }, []);

  if (!s) {
    if (state.status === "error") {
      return (
        <div className="session-load">
          <EmptyState
            title={t("error.generic")}
            body={<ErrorNotice error={state.error} />}
            action={
              <Link to="/sessions" className="button secondary">
                <ArrowLeft size={14} />
                {t("session.all")}
              </Link>
            }
          />
        </div>
      );
    }
    return (
      <div className="wb wb--loading" aria-busy="true" aria-label={t("common.loading")}>
        <header className="wb-head">
          <Skeleton width={220} height={16} />
        </header>
        <div className="wb-grid">
          <section className="wb-chat">
            <div className="conv">
              <div className="conv__scroll">
                <div className="conv__content conv__content--skeleton">
                  <Skeleton width="60%" height={64} />
                  <Skeleton width="40%" />
                  <Skeleton width="70%" />
                  <Skeleton width="30%" />
                </div>
              </div>
            </div>
          </section>
          <aside className="wb-panel">
            <nav className="wpanel__tabs" aria-hidden="true">
              {PANELS.slice(0, 4).map((x) => (
                <span key={x} className="wpanel__tab">
                  <Skeleton width={64} />
                </span>
              ))}
            </nav>
          </aside>
        </div>
      </div>
    );
  }

  const liveTurn = state.turns.find((turn) => isLiveTurn(turn));
  const status = statusOf(s, liveTurn, t);
  const go = (next: Panel | "conversation") => nav(next === "conversation" ? `/sessions/${id}` : `/sessions/${id}/${next}`);
  const repo = s.worktree?.repository ?? null;
  const base = s.worktree?.base_sha ? s.worktree.base_sha.slice(0, 7) : null;

  return (
    <div className={`wb ${showPanel ? "wb--panel" : "wb--chat"}${panelOpen ? "" : " wb--closed"}`}>
      <header className="wb-head">
        <IconButton className="wb-back" label={t("session.back")} icon={<ArrowLeft size={16} />} onClick={() => nav("/sessions")} />
        <h1 className="wb-head__title truncate" title={s.title || t("session.untitled")}>
          {s.title || t("session.untitled")}
        </h1>
        <span className="wb-head__status" data-testid="session-status">
          <Pill tone={status.tone}>{status.label}</Pill>
        </span>
        <span className="wb-head__meta truncate">
          <span className="wb-head__chip">
            {harnessName(s.harness.provider_id)}
            {s.harness.model ? ` · ${s.harness.model}` : ""}
            {s.harness.effort ? ` · ${s.harness.effort}` : ""}
          </span>
          {repo ? (
            <span className="wb-head__chip wb-head__chip--repo" title={`${repo}${base ? `@${base}` : ""}`}>
              <Github size={12} />
              {repo}
              {base ? `@${base}` : ""}
            </span>
          ) : null}
          {s.parent_session_id ? (
            <Link className="wb-head__chip" to={`/sessions/${s.parent_session_id}`}>
              {t("session.parent")}
            </Link>
          ) : null}
        </span>
        <span className="wb-head__right">
          <IconButton
            className="wb-panel-toggle"
            label={t("session.panel_toggle")}
            icon={<PanelRight size={16} />}
            aria-pressed={panelOpen}
            onClick={togglePanel}
          />
          <Menu
            align="end"
            label={t("session.actions")}
            trigger={<IconButton label={t("session.actions")} icon={<MoreHorizontal size={16} />} />}
          >
            {s.actions.includes("archive") ? (
              <MenuItem label={t("session.archive")} onSelect={() => void lifecycle.run("archive")} />
            ) : null}
            {s.actions.includes("unarchive") ? (
              <MenuItem label={t("session.unarchive")} onSelect={() => void lifecycle.run("unarchive")} />
            ) : null}
            {s.actions.includes("close") ? (
              <MenuItem label={t("session.close")} danger onSelect={() => void lifecycle.run("close")} />
            ) : null}
            <MenuItem
              label={t("session.copy_id")}
              onSelect={() => {
                void navigator.clipboard?.writeText(s.id).then(() => toast({ text: t("session.copied"), tone: "info" }));
              }}
            />
            {s.parent_session_id ? <MenuSeparator /> : null}
            {s.parent_session_id ? (
              <MenuItem label={t("session.parent_open")} onSelect={() => nav(`/sessions/${s.parent_session_id}`)} />
            ) : null}
          </Menu>
        </span>
      </header>
      <ErrorNotice error={lifecycle.error} />

      <div className="wb-switch" role="tablist" aria-label={t("session.sections")}>
        <button type="button" role="tab" aria-selected={!showPanel} onClick={() => go("conversation")}>
          {t("tab.conversation")}
        </button>
        <button type="button" role="tab" aria-selected={showPanel} onClick={() => go(panel)}>
          {t("session.workspace")}
        </button>
      </div>

      <div className="wb-grid" style={{ "--wb-panel-frac": split } as React.CSSProperties}>
        <section className="wb-chat" aria-label={t("tab.conversation")}>
          <ConversationPane session={s} live={live} state={state} />
        </section>
        <div
          className="wb-divider"
          role="separator"
          aria-orientation="vertical"
          aria-label={t("session.resize")}
          onPointerDown={onDividerDown}
        />
        <aside className="wb-panel" aria-label={t("session.workspace")}>
          <WorkspacePanel session={s} live={live} state={state} panel={panel} onSelect={go} />
        </aside>
      </div>
    </div>
  );
}

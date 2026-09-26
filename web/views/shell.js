import { api } from "../lib/api.js";
import { clearConnection, getConnection, planeLabel } from "../lib/config.js";
import { h, mount } from "../lib/dom.js";
import { getLang, setLang, t } from "../lib/i18n.js";
import { icon, logo } from "../lib/icons.js";
import { navigate } from "../lib/router.js";
import { getTheme, setTheme } from "../lib/store.js";
import { button, poller } from "../lib/ui.js";

const DOCS_BASE = typeof window.SBX_DOCS_URL === "string" ? window.SBX_DOCS_URL.trim().replace(/\/+$/, "") : "";
const DOCS_SOURCE = "https://github.com/soren-labs/sbx-browser/tree/main/docs-site";

/**
 * Link to a docs-site page such as `getting-started/quick-start`. Deep links
 * (and the zh-cn locale) need a hosted site in `window.SBX_DOCS_URL`; without
 * one, every link falls back to the docs sources on GitHub.
 */
export function docsUrl(page = "") {
  if (!DOCS_BASE) return DOCS_SOURCE;
  const locale = getLang() === "zh-CN" ? "zh-cn/" : "";
  return `${DOCS_BASE}/${locale}${page ? `${page}/` : ""}`;
}

// Primary product surface: what you came to do.
const NAV = [
  { id: "home", label: "Home", iconName: "home", href: "#/", match: ["home"] },
  { id: "tasks", label: "Tasks", iconName: "listTodo", href: "#/tasks", match: ["tasks", "task", "task-new"] },
  { id: "integrations", label: "Integrations", iconName: "plug", href: "#/integrations", match: ["integrations", "github", "accounts", "capacity"] },
  { id: "settings", label: "Settings", iconName: "settings", href: "#/settings", match: ["settings", "keys"] },
];

// Raw primitives — still first-class routes, one level down.
const ADVANCED_NAV = [
  { id: "agents", label: "Agents", iconName: "bot", href: "#/agents", match: ["agents", "agent", "agent-new"] },
  { id: "workflows", label: "Workflows", iconName: "workflow", href: "#/workflows", match: ["workflows", "workflow"] },
  { id: "artifacts", label: "Artifacts", iconName: "package", href: "#/artifacts", match: ["artifacts", "artifact"] },
];

const THEMES = [
  ["system", "monitor", "System theme"],
  ["light", "sun", "Light theme"],
  ["dark", "moon", "Dark theme"],
];

export function createShell() {
  const main = h("main", { class: "main", id: "main" });
  const navEl = h("nav", { class: "nav", "aria-label": t("Primary") });
  const identityEl = h("div");
  const toolsEl = h("div", { class: "sidebar-tools" });
  let active = null;
  let liveCount = 0;

  const root = h("div", { class: "shell", "data-testid": "app-ready" });
  const closeNav = () => root.classList.remove("nav-open");

  const link = (item, locked) =>
    h(
      "a",
      {
        class: ["nav-link", item.match.includes(active) && "is-active", locked && "is-locked"],
        href: item.href,
        "data-testid": `nav-${item.id}`,
        title: locked ? t("Requires an API key with the admin scope") : null,
        onClick: closeNav,
      },
      icon(item.iconName),
      h("span", null, t(item.label)),
      locked ? icon("lock", { size: 13, className: "muted-icon" }) : null,
      item.id === "tasks" && liveCount ? h("span", { class: "nav-count", title: t("Active tasks") }, String(liveCount)) : null,
    );

  function renderNav() {
    mount(
      navEl,
      NAV.map((item) => link(item, false)),
      h("div", { class: "nav-section" }, t("Advanced")),
      ADVANCED_NAV.map((item) => link(item, false)),
    );
  }

  function renderIdentity() {
    const idn = getConnection().identity;
    mount(
      identityEl,
      h(
        "div",
        { class: "identity", "data-testid": "identity" },
        h("span", { class: "identity-avatar" }, icon("key", { size: 14 })),
        h(
          "div",
          { class: "identity-text" },
          h("div", { class: "identity-label" }, idn?.label || idn?.key_id || t("API key")),
          h("div", { class: "subtle" }, (idn?.scopes || []).join(" · ") || "—"),
        ),
      ),
    );
  }

  function renderTools() {
    const theme = getTheme();
    const next = THEMES[(THEMES.findIndex(([v]) => v === theme) + 1) % THEMES.length];
    const current = THEMES.find(([v]) => v === theme);
    mount(
      toolsEl,
      button("", {
        variant: "ghost",
        size: "sm",
        iconName: current[1],
        title: `${t(current[2])} — ${t("click for")} ${t(next[2]).toLowerCase()}`,
        testid: "theme-toggle",
        onClick: () => {
          setTheme(next[0]);
          renderTools();
        },
      }),
      button("", {
        variant: "ghost",
        size: "sm",
        iconName: "languages",
        title: getLang() === "zh-CN" ? "Switch to English" : "切换到中文",
        testid: "lang-toggle",
        onClick: () => setLang(getLang() === "zh-CN" ? "en" : "zh-CN"),
      }),
      h(
        "a",
        { class: "btn btn-ghost btn-sm btn-icon", href: docsUrl(), target: "_blank", rel: "noopener noreferrer", title: t("Documentation"), "data-testid": "nav-docs" },
        icon("book"),
      ),
      button("", {
        variant: "ghost",
        size: "sm",
        iconName: "logOut",
        title: t("Disconnect"),
        testid: "disconnect",
        onClick: () => {
          clearConnection();
          navigate("/connect");
        },
      }),
    );
  }

  const sidebar = h(
    "aside",
    { class: "sidebar" },
    h(
      "div",
      { class: "sidebar-top" },
      h(
        "a",
        { class: "brand", href: "#/" },
        logo(28),
        h("span", null, h("span", { class: "brand-name" }, "sbx-browser"), h("span", { class: "brand-plane", title: planeLabel() }, planeLabel())),
      ),
      h(
        "button",
        {
          type: "button",
          class: "nav-close",
          "aria-label": t("Close menu"),
          "data-testid": "nav-close",
          onClick: closeNav,
        },
        icon("x"),
      ),
    ),
    navEl,
    h("div", { class: "sidebar-foot" }, identityEl, toolsEl),
  );

  const mobileBar = h(
    "div",
    { class: "mobile-bar" },
    button("", { variant: "ghost", size: "sm", iconName: "menu", title: t("Menu"), testid: "nav-menu", onClick: () => root.classList.toggle("nav-open") }),
    logo(22),
    h("strong", null, "sbx-browser"),
  );

  const scrim = h("div", { class: "nav-scrim", "data-testid": "nav-scrim", onClick: closeNav });
  const onKey = (ev) => {
    if (ev.key === "Escape" && root.classList.contains("nav-open")) closeNav();
  };
  document.addEventListener("keydown", onKey);

  mount(root, scrim, sidebar, h("div", { style: "min-width:0;display:grid;grid-template-rows:auto minmax(0,1fr);height:100%" }, mobileBar, main));
  renderNav();
  renderIdentity();
  renderTools();

  // Live-task badge on the nav: the cheap /v1/agents/summary rollup, not
  // a full list fetch. View actions call bumpLive() for immediate
  // refresh; the 30s tick is only the fallback while events are absent.
  const live = poller(async () => {
    if (!root.isConnected) {
      live.stop();
      document.removeEventListener("keydown", onKey);
      return;
    }
    const res = await api.agentsSummary();
    const counts = res?.by_status || {};
    const count = (counts.running || 0) + (counts.creating || 0);
    if (count !== liveCount) {
      liveCount = count;
      renderNav();
    }
  }, 30000);
  // root is attached synchronously right after createShell() returns; a bare
  // now() here would trip the not-connected guard and stop the poller.
  queueMicrotask(() => live.now());

  return {
    el: root,
    main,
    setActive(name) {
      active = name;
      renderNav();
    },
    setContent(el) {
      mount(main, el);
    },
    refreshIdentity() {
      renderIdentity();
    },
    bumpLive() {
      live.now();
    },
  };
}

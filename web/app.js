import { api } from "./lib/api.js";
import { clearConnection, getConnection, isConnected, saveConnection } from "./lib/config.js";
import { h, mount } from "./lib/dom.js";
import { onLangChange, t } from "./lib/i18n.js";
import { navigate, parseHash } from "./lib/router.js";
import { applyTheme } from "./lib/store.js";
import { toast } from "./lib/ui.js";
import { renderConnect } from "./views/connect.js";
import { createShell } from "./views/shell.js";
import { VIEWS } from "./views/index.js";

applyTheme();

const app = document.getElementById("app");
let shell = null;
let view = null;
let identityChecked = false;

const GH_CALLBACK_KEY = "sbx.console.github_callback";

/** GitHub App setup redirects to `/?installation_id=…&state=…`; stash and route. */
function captureGithubCallback() {
  const params = new URLSearchParams(window.location.search);
  const installationId = params.get("installation_id");
  if (!installationId) return;
  sessionStorage.setItem(
    GH_CALLBACK_KEY,
    JSON.stringify({
      installation_id: installationId,
      state: params.get("state") || "",
      setup_action: params.get("setup_action") || "",
    }),
  );
  history.replaceState(null, "", `${window.location.pathname}#/admin/github`);
}

function disposeView() {
  try {
    view?.dispose?.();
  } catch {
    // A failing teardown must not block navigation.
  }
  view = null;
}

async function refreshIdentity() {
  const conn = getConnection();
  try {
    const me = await api.me();
    saveConnection({ ...conn, identity: me });
    shell?.refreshIdentity();
  } catch {
    // 401 is handled by the global unauthorized listener.
  }
}

async function route() {
  const r = parseHash();
  if (!isConnected() && r.name !== "connect") {
    navigate("/connect", r.path && r.path !== "/agents" ? { next: r.path } : undefined, { replace: true });
    return;
  }
  disposeView();
  if (r.name === "connect") {
    shell = null;
    view = renderConnect({ route: r, onConnected: (next) => navigate(next || "/agents") });
    mount(app, view.el);
    return;
  }
  if (!shell) {
    shell = createShell();
    mount(app, shell.el);
  }
  if (!identityChecked) {
    identityChecked = true;
    void refreshIdentity();
  }
  shell.setActive(r.name);
  const factory = VIEWS[r.name] || VIEWS["not-found"];
  view = factory({ route: r, shell });
  shell.setContent(view.el);
  shell.main.scrollTop = 0;
  document.title = view.title ? `${view.title} · sbx-browser` : "sbx-browser";
}

window.addEventListener("hashchange", () => void route());

window.addEventListener("sbx:unauthorized", () => {
  if (parseHash().name === "connect") return;
  const conn = getConnection();
  clearConnection();
  saveConnection({ baseUrl: conn.baseUrl, apiKey: "", remember: conn.remember });
  toast(t("Your API key was rejected. Connect again."), { tone: "danger" });
  identityChecked = false;
  navigate("/connect", { reason: "unauthorized" });
});

onLangChange(() => {
  shell = null;
  void route();
});

captureGithubCallback();
mount(app, h("div", { class: "boot" }, t("Loading…")));
void route();

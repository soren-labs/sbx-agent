import { api } from "../lib/api.js";
import { defaultBaseUrl, getConnection, saveConnection } from "../lib/config.js";
import { h } from "../lib/dom.js";
import { explainApiError } from "../lib/domain.js";
import { getLang, setLang, t } from "../lib/i18n.js";
import { logo } from "../lib/icons.js";
import { getTheme, setTheme } from "../lib/store.js";
import { banner, button, field, rich, toggle } from "../lib/ui.js";
import { docsUrl } from "./shell.js";

export function renderConnect({ route, onConnected }) {
  const conn = getConnection();
  const state = {
    apiKey: "",
    baseUrl: conn.baseUrl || defaultBaseUrl(),
    remember: conn.remember,
    advanced: Boolean(conn.baseUrl),
    busy: false,
  };
  const errorSlot = h("div");
  if (route.query.reason === "unauthorized") {
    errorSlot.append(
      banner({ tone: "warning", title: t("Your API key was rejected."), body: t("It may have been revoked. Paste a valid key to continue.") }),
    );
  }

  const keyInput = h("input", {
    class: "input mono",
    id: "api-key",
    type: "password",
    placeholder: "sbx_…",
    autocomplete: "off",
    spellcheck: "false",
    required: true,
    "data-testid": "connect-key",
    onInput: (ev) => {
      state.apiKey = ev.target.value;
    },
  });
  const urlInput = h("input", {
    class: "input mono",
    id: "base-url",
    type: "url",
    placeholder: window.location.origin,
    value: state.baseUrl,
    "data-testid": "connect-url",
    onInput: (ev) => {
      state.baseUrl = ev.target.value;
    },
  });
  const advancedField = field(t("Control plane URL"), urlInput, {
    htmlFor: "base-url",
    hint: t("Leave empty when the console is served by your control plane or edge (same origin)."),
  });
  advancedField.hidden = !state.advanced;

  const submit = button(t("Connect"), { variant: "primary", type: "submit", testid: "connect-submit" });
  submit.style.width = "100%";

  const form = h(
    "form",
    {
      class: "fields",
      onSubmit: async (ev) => {
        ev.preventDefault();
        if (state.busy) return;
        const key = state.apiKey.trim();
        if (!key) {
          keyInput.focus();
          return;
        }
        state.busy = true;
        submit.disabled = true;
        errorSlot.replaceChildren();
        // Probe with the candidate URL before persisting anything.
        const prev = getConnection();
        saveConnection({ baseUrl: state.advanced ? state.baseUrl : "", apiKey: "", remember: state.remember });
        try {
          const me = await api.me(key);
          saveConnection({ baseUrl: state.advanced ? state.baseUrl : "", apiKey: key, remember: state.remember, identity: me });
          onConnected(route.query.next);
        } catch (err) {
          saveConnection({ ...prev, apiKey: "" });
          const info = explainApiError(err);
          const title =
            err.status === 401
              ? t("This key was rejected by the control plane.")
              : err.status === 0
                ? t("Could not reach the control plane.")
                : info.title;
          errorSlot.replaceChildren(banner({ tone: "danger", title, body: h("span", { class: "muted" }, info.detail || info.code), testid: "connect-error" }));
        } finally {
          state.busy = false;
          submit.disabled = false;
        }
      },
    },
    errorSlot,
    field(t("API key"), keyInput, { htmlFor: "api-key", required: true }),
    advancedField,
    toggle(t("Remember on this device"), state.remember, (v) => {
      state.remember = v;
    }, { hint: t("Otherwise the key is forgotten when this tab closes.") }),
    submit,
    h(
      "button",
      {
        type: "button",
        class: "btn btn-ghost btn-sm",
        style: "justify-self:center",
        onClick: (ev) => {
          state.advanced = !state.advanced;
          advancedField.hidden = !state.advanced;
          ev.currentTarget.textContent = state.advanced ? t("Use this origin") : t("Use a different control plane URL");
        },
      },
      state.advanced ? t("Use this origin") : t("Use a different control plane URL"),
    ),
  );

  const el = h(
    "div",
    { class: "connect", "data-testid": "connect-view" },
    h(
      "div",
      null,
      h(
        "div",
        { class: "connect-card" },
        h(
          "div",
          { class: "connect-head" },
          logo(40),
          h("h1", null, t("Connect to your control plane")),
          h("p", { class: "muted" }, t("sbx-browser runs coding agents in isolated sandboxes on your own Modal workspace. Paste an API key to manage them.")),
        ),
        form,
        h(
          "div",
          { class: "connect-foot" },
          h("strong", null, t("Where do I get a key?")),
          h("span", null, rich(t("`sbx deploy` writes an admin key to")), " ", h("code", null, "~/.local/state/sbx/bootstrap.key"), "."),
          h("span", null, t("Admins can mint more keys under Admin → API keys.")),
          h("a", { href: docsUrl("getting-started/quick-start"), target: "_blank", rel: "noopener noreferrer" }, t("Read the quick start →")),
        ),
      ),
      h(
        "div",
        { class: "connect-tools" },
        button(getLang() === "zh-CN" ? "English" : "中文", { variant: "ghost", size: "sm", iconName: "languages", onClick: () => setLang(getLang() === "zh-CN" ? "en" : "zh-CN") }),
        button(t("Theme"), {
          variant: "ghost",
          size: "sm",
          iconName: getTheme() === "dark" ? "moon" : getTheme() === "light" ? "sun" : "monitor",
          onClick: () => setTheme(getTheme() === "dark" ? "light" : getTheme() === "light" ? "system" : "dark"),
        }),
      ),
    ),
  );
  queueMicrotask(() => keyInput.focus());
  return { el, title: t("Connect") };
}

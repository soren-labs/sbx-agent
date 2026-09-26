import { clearConnection, getConnection, planeLabel } from "../lib/config.js";
import { h } from "../lib/dom.js";
import { getLang, setLang, t } from "../lib/i18n.js";
import { navigate } from "../lib/router.js";
import { getTheme, prompts, recentWorkflows, setTheme } from "../lib/store.js";
import { badge, button, card, kv, pageHeader, segmented, toast } from "../lib/ui.js";

export function renderSettings() {
  const conn = getConnection();
  const idn = conn.identity || {};
  const el = h(
    "div",
    { class: "page page-narrow" },
    pageHeader({ title: t("Settings"), subtitle: t("Stored in this browser only."), testid: "page-title" }),
    h(
      "div",
      { class: "stack" },
      card({
        title: t("Connection"),
        iconName: "server",
        body: kv([
          [t("Control plane"), h("code", null, conn.baseUrl || `${window.location.origin} (${t("same origin")})`)],
          [t("Key label"), idn.label || "—"],
          [t("Key id"), idn.key_id ? h("code", null, idn.key_id) : null],
          [t("Scopes"), (idn.scopes || []).map((s) => badge(s, { tone: s === "admin" ? "violet" : "neutral" }))],
          [t("Stored in"), conn.remember ? t("this device (localStorage)") : t("this tab (sessionStorage)")],
        ]),
        footer: [
          button(t("Switch key"), {
            onClick: () => {
              clearConnection();
              navigate("/connect");
            },
          }),
        ],
      }),
      card({
        title: t("Appearance"),
        iconName: "sun",
        body: h(
          "div",
          { class: "fields" },
          h(
            "div",
            { class: "spread" },
            h("span", null, t("Theme")),
            segmented(
              [
                { value: "system", label: t("System"), iconName: "monitor" },
                { value: "light", label: t("Light"), iconName: "sun" },
                { value: "dark", label: t("Dark"), iconName: "moon" },
              ],
              getTheme(),
              (v) => setTheme(v),
              { testid: "settings-theme" },
            ),
          ),
          h(
            "div",
            { class: "spread" },
            h("span", null, t("Language")),
            segmented(
              [
                { value: "en", label: "English" },
                { value: "zh-CN", label: "简体中文" },
              ],
              getLang(),
              (v) => setLang(v),
              { testid: "settings-lang" },
            ),
          ),
        ),
      }),
      card({
        title: t("API keys"),
        iconName: "key",
        subtitle: t("Mint keys for CI or share one with a teammate — scoped to agents or admin."),
        body: h("a", { class: "btn", href: "#/settings/keys", "data-testid": "settings-keys" }, t("Manage API keys")),
      }),
      card({
        title: t("Local data"),
        iconName: "trash",
        body: h("p", { class: "muted" }, t("The console remembers prompts it sent (the API does not store them) and recently viewed workflows.")),
        footer: [
          button(t("Clear local data"), {
            variant: "danger",
            onClick: () => {
              prompts.clear();
              recentWorkflows.clear();
              toast(t("Local data cleared"), { tone: "success" });
            },
          }),
        ],
      }),
      h("p", { class: "subtle", style: "font-size:12px;text-align:center" }, `sbx-browser console · ${planeLabel()}`),
    ),
  );
  return { el, title: t("Settings") };
}

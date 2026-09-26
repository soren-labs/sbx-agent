import { clearConnection, getConnection, hasScope, planeLabel } from "../lib/config.js";
import { h } from "../lib/dom.js";
import { getLang, setLang, t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { navigate } from "../lib/router.js";
import { getTheme, prompts, recentWorkflows, setTheme } from "../lib/store.js";
import { badge, button, card, kv, pageHeader, segmented, toast } from "../lib/ui.js";

/** Operator surface entry — product integrations live under Integrations;
 *  these pages are deployment-level diagnostics and key administration. */
function adminRow({ title, body, href, testid, locked }) {
  return h(
    "a",
    {
      class: "admin-row",
      href,
      "data-testid": testid,
      style: "display:flex;align-items:center;gap:12px;padding:10px 0;border-top:1px solid var(--border);text-decoration:none;color:inherit",
    },
    h("div", { style: "flex:1;min-width:0" }, h("div", { class: "cell-title" }, title), h("span", { class: "cell-sub" }, body)),
    locked
      ? h("span", { class: "subtle", style: "display:inline-flex;align-items:center;gap:4px" }, icon("lock", { size: 13 }), t("Admin scope"))
      : icon("arrowRight", { size: 16, className: "muted-icon" }),
  );
}

export function renderSettings() {
  const conn = getConnection();
  const idn = conn.identity || {};
  const admin = hasScope("admin");
  const el = h(
    "div",
    { class: "page page-narrow" },
    pageHeader({ title: t("Settings"), subtitle: t("Console preferences, plus administration when your key has the admin scope."), testid: "page-title" }),
    h(
      "div",
      { class: "stack" },
      h("div", { class: "section-title" }, t("This browser")),
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
      h("div", { class: "section-title" }, t("Administration"), h("span", { class: "subtle" }, " · ", t("deployment-wide operator surface"))),
      card({
        title: t("Administration"),
        iconName: "shield",
        subtitle: t("Operator diagnostics and key management — product integrations live under Integrations."),
        testid: "admin-section",
        body: h(
          "div",
          null,
          adminRow({
            title: t("API keys"),
            body: t("Mint keys for CI or share one with a teammate — scoped to agents or admin."),
            href: "#/settings/keys",
            testid: "settings-keys",
            locked: !admin,
          }),
          adminRow({
            title: t("Capacity"),
            body: t("Live provider slots — what can actually take a run right now."),
            href: "#/settings/capacity",
            testid: "settings-capacity",
            locked: false,
          }),
          adminRow({
            title: t("Provider runtime"),
            body: t("Deploy evidence per provider: enabled state, image, version and last deploy detail."),
            href: "#/settings/runtime",
            testid: "settings-runtime",
            locked: !admin,
          }),
        ),
      }),
      h("p", { class: "subtle", style: "font-size:12px;text-align:center" }, `sbx-browser console · ${planeLabel()}`),
    ),
  );
  return { el, title: t("Settings") };
}

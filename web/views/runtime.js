import { api } from "../lib/api.js";
import { h, mount } from "../lib/dom.js";
import { runtimeMeta } from "../lib/domain.js";
import { fmtDateTime, fmtRelative } from "../lib/format.js";
import { t } from "../lib/i18n.js";
import { badge, card, emptyState, errorBanner, labelize, pageHeader, providerTag, skeleton } from "../lib/ui.js";
import { adminGate } from "./admin-gate.js";

/** Deploy evidence per provider — what `sbx deploy` last reported.
 *  Operator diagnostics, kept under Settings rather than Integrations. */
export function renderRuntime() {
  const gate = adminGate(t("Provider runtime"), t("Deploy evidence per provider — image, version and last deploy status."));
  if (gate) return gate;
  const body = h("div", null, h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(5))));

  async function load() {
    try {
      const res = await api.listProviders();
      const rows = res.providers || [];
      mount(
        body,
        rows.length
          ? h(
              "div",
              { class: "table-wrap", style: "border:0" },
              labelize(
                h(
                "table",
                { class: "table", "data-testid": "runtime-table" },
                h("thead", null, h("tr", null, [t("Provider"), t("Runtime"), t("Image"), t("Version"), t("Detail"), t("Last deploy")].map((c) => h("th", null, c)))),
                h(
                  "tbody",
                  null,
                  rows.map((r) => {
                    const rt = r.runtime || {};
                    const meta = runtimeMeta(rt.status);
                    return h(
                      "tr",
                      { "data-testid": `runtime-${r.provider}` },
                      h("td", null, h("div", { class: "cell-title" }, providerTag(r.provider)), h("span", { class: "cell-sub" }, r.distribution?.kind || "")),
                      h("td", null, badge(meta.label, { tone: meta.tone })),
                      h("td", null, rt.image ? h("code", { class: "mono", style: "font-size:12px" }, rt.image) : h("span", { class: "subtle" }, "—")),
                      h("td", { class: "muted nowrap" }, rt.version || "—"),
                      h("td", { class: "muted", style: "max-width:320px" }, rt.detail || "—"),
                      h("td", { class: "muted nowrap", title: rt.updated_at ? fmtDateTime(rt.updated_at) : "" }, rt.updated_at ? fmtRelative(rt.updated_at) : "—"),
                    );
                  }),
                ),
                )),
            )
          : emptyState({ iconName: "server", title: t("No providers"), body: t("Nothing is configured in this deployment."), compact: true }),
      );
    } catch (err) {
      mount(body, errorBanner(err, { retry: load }));
    }
  }

  const el = h(
    "div",
    { class: "page", "data-testid": "runtime-view" },
    pageHeader({
      title: t("Provider runtime"),
      subtitle: t("Deploy evidence per provider — what each provider image reported at its last deploy."),
      back: { href: "#/settings", label: t("Settings") },
      testid: "page-title",
    }),
    card({ body }),
  );
  void load();
  return { el, title: t("Provider runtime") };
}

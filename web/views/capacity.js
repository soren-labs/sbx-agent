import { api } from "../lib/api.js";
import { hasScope } from "../lib/config.js";
import { h, mount } from "../lib/dom.js";
import { CANONICAL_EFFORTS, PROVIDER_META, PROVIDERS, providerLabel } from "../lib/domain.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { badge, card, emptyState, errorBanner, pageHeader, poller, progressBar, providerTag, skeleton, statusBadge } from "../lib/ui.js";

export function renderCapacity() {
  const body = h("div", null, skeleton(6));

  async function load() {
    let models;
    let accounts = null;
    try {
      models = (await api.models()).models || [];
      if (hasScope("admin")) accounts = (await api.listAccounts()).accounts || [];
    } catch (err) {
      mount(body, errorBanner(err, { retry: load }));
      return;
    }
    const providers = PROVIDERS.filter((p) => models.some((m) => m.provider === p) || accounts?.some((a) => a.provider === p));
    if (!providers.length) {
      mount(body, emptyState({ iconName: "gauge", title: t("No providers are enabled"), body: t("Enable providers at deploy time (SBX_PROVIDERS) and import at least one account per provider.") }));
      return;
    }
    mount(
      body,
      h(
        "div",
        { class: "grid-2" },
        providers.map((p) => {
          const pm = models.filter((m) => m.provider === p);
          const pa = accounts ? accounts.filter((a) => a.provider === p) : null;
          // SOR-204: rows are per (account, model) — union the advertised
          // efforts, ordered by the canonical ladder.
          const efforts = CANONICAL_EFFORTS.filter((e) =>
            pm.some((m) => (m.reasoning_efforts || []).includes(e)),
          );
          const tier = PROVIDER_META[p]?.tier;
          return card({
            class: "provider-card",
            testid: `capacity-${p}`,
            title: h("span", { class: "row", style: "gap:8px" }, providerTag(p), badge(tier === "stable" ? t("Stable") : t("Experimental"), { tone: tier === "stable" ? "green" : "amber" })),
            actions: efforts.length ? badge(`${t("effort")}: ${efforts.join(" / ")}`) : badge(t("no effort setting")),
            body: h(
              "div",
              { class: "stack", style: "gap:14px" },
              h(
                "div",
                null,
                h("div", { class: "subtle", style: "font-size:12px;font-weight:600;margin-bottom:4px" }, t("Models")),
                pm.length
                  ? pm.map((m) =>
                      h(
                        "div",
                        { class: "model-row" },
                        h("code", null, m.model),
                        m.accounts_available ? badge(t("{n} free", { n: m.accounts_available }), { tone: "green" }) : badge(t("no free account"), { tone: "amber" }),
                      ),
                    )
                  : h("p", { class: "muted" }, t("No account advertises a model.")),
              ),
              pa
                ? h(
                    "div",
                    null,
                    h("div", { class: "subtle", style: "font-size:12px;font-weight:600;margin-bottom:4px" }, t("Accounts")),
                    pa.length
                      ? pa.map((a) =>
                          h(
                            "div",
                            { class: "model-row" },
                            h("div", { class: "cell-title" }, h("strong", null, a.label), h("span", { class: "cell-sub mono" }, a.id)),
                            h("div", { class: "row", style: "min-width:160px;justify-content:flex-end" }, statusBadge("account", a.status), h("span", { class: "subtle", style: "font-size:12px" }, `${a.running}/${a.max_concurrent}`), h("div", { style: "width:70px" }, progressBar(a.running, a.max_concurrent, { tone: a.running >= a.max_concurrent ? "amber" : "accent" }))),
                          ),
                        )
                      : h("p", { class: "muted" }, t("No accounts imported.")),
                  )
                : null,
            ),
          });
        }),
      ),
      !hasScope("admin")
        ? h("p", { class: "subtle", style: "margin-top:14px;font-size:12.5px" }, icon("lock", { size: 12 }), " ", t("Per-account slot usage is visible to admin keys."))
        : null,
    );
  }

  const el = h(
    "div",
    { class: "page page-wide" },
    pageHeader({
      title: t("Capacity"),
      subtitle: t("Models each provider offers and how many accounts have a free slot right now. `auto` scheduling picks the least-recently-used free account."),
      back: { href: "#/integrations", label: t("Integrations") },
      testid: "page-title",
    }),
    body,
  );
  void load();
  const poll = poller(load, 10000);
  return { el, title: t("Capacity"), dispose: () => poll.stop(), providerLabel };
}

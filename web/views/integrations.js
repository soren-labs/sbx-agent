import { api } from "../lib/api.js";
import { hasScope } from "../lib/config.js";
import { h, mount } from "../lib/dom.js";
import { PROVIDERS, providerLabel } from "../lib/domain.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { badge, card, pageHeader } from "../lib/ui.js";

function hubCard({ iconName, title, body, href, statusEl, locked, testid }) {
  return h(
    "a",
    { class: ["hub-card", locked && "is-locked"], href, "data-testid": testid },
    h("span", { class: "hub-card-icon" }, icon(iconName, { size: 20 })),
    h(
      "span",
      { class: "hub-card-body" },
      h("span", { class: "hub-card-title" }, title, locked ? icon("lock", { size: 13, className: "muted-icon" }) : null),
      h("span", { class: "muted" }, body),
      statusEl,
    ),
    icon("chevronRight", { className: "muted-icon" }),
  );
}

export function renderIntegrations() {
  const admin = hasScope("admin");

  const githubStatus = h("span", { class: "hub-card-status" }, h("span", { class: "subtle" }, t("Checking…")));
  const accountsStatus = h("span", { class: "hub-card-status" }, h("span", { class: "subtle" }, admin ? t("Checking…") : t("Admin scope required")));
  const capacityStatus = h("span", { class: "hub-card-status" }, h("span", { class: "subtle" }, t("Checking…")));

  const el = h(
    "div",
    { class: "page page-narrow", "data-testid": "integrations-view" },
    pageHeader({
      title: t("Integrations"),
      subtitle: t("Connect the providers and code hosts your agents run on."),
      testid: "page-title",
    }),
    h(
      "div",
      { class: "hub-grid" },
      hubCard({
        iconName: "github",
        title: t("GitHub"),
        body: t("Private repositories, branch push and pull-request delivery."),
        href: "#/integrations/github",
        statusEl: githubStatus,
        locked: !admin,
        testid: "int-github",
      }),
      hubCard({
        iconName: "users",
        title: t("Provider accounts"),
        body: t("The provider logins the scheduler picks from — Codex, Devin, Grok and more."),
        href: "#/integrations/accounts",
        statusEl: accountsStatus,
        locked: !admin,
        testid: "int-accounts",
      }),
      hubCard({
        iconName: "gauge",
        title: t("Capacity"),
        body: t("Which providers and models have a free account slot right now."),
        href: "#/integrations/capacity",
        statusEl: capacityStatus,
        testid: "int-capacity",
      }),
    ),
    card({
      title: t("API keys"),
      iconName: "key",
      subtitle: t("Mint keys for CI or share one with a teammate — scoped to agents or admin."),
      body: h("a", { class: "btn", href: "#/settings/keys", "data-testid": "int-keys" }, t("Manage API keys")),
    }),
  );

  void (async () => {
    try {
      const st = await api.githubStatus();
      if (!githubStatus.isConnected) return;
      mount(
        githubStatus,
        st.configured
          ? badge(t("Connected"), { tone: "green" })
          : badge(t("Not connected"), { tone: "amber" }),
      );
    } catch {
      mount(githubStatus, h("span", { class: "subtle" }, t("Unavailable")));
    }
  })();

  if (admin) {
    void (async () => {
      try {
        const res = await api.listAccounts();
        const accounts = res.accounts || [];
        const active = accounts.filter((a) => a.status === "active").length;
        if (!accountsStatus.isConnected) return;
        mount(
          accountsStatus,
          badge(t("{n} active", { n: active }), { tone: active ? "green" : "amber" }),
          h("span", { class: "subtle" }, ` ${t("of {n}", { n: accounts.length })}`),
        );
      } catch {
        mount(accountsStatus, h("span", { class: "subtle" }, t("Unavailable")));
      }
    })();
  }

  void (async () => {
    try {
      const res = await api.models();
      const models = res.models || [];
      const freeProviders = new Set(models.filter((m) => (m.accounts_available || 0) > 0).map((m) => m.provider));
      if (!capacityStatus.isConnected) return;
      mount(
        capacityStatus,
        freeProviders.size
          ? badge(t("{n} provider(s) free", { n: freeProviders.size }), { tone: "green" })
          : badge(t("All providers busy"), { tone: "amber" }),
        h("span", { class: "subtle" }, ` ${freeProviders.size ? PROVIDERS.filter((p) => freeProviders.has(p)).map(providerLabel).join(", ") : ""}`),
      );
    } catch {
      mount(capacityStatus, h("span", { class: "subtle" }, t("Unavailable")));
    }
  })();

  return { el, title: t("Integrations") };
}

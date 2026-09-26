import { api } from "../lib/api.js";
import { hasScope } from "../lib/config.js";
import { h, mount } from "../lib/dom.js";
import { connectionMeta, providerLabel } from "../lib/domain.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { badge, banner, button, card, errorBanner, labelize, pageHeader, providerTag, skeleton } from "../lib/ui.js";
import { connectDialog } from "./accounts.js";

function githubBadge(st) {
  if (!st?.configured) return badge(t("Not connected"), { tone: "neutral" });
  const suspended = (st.installations || []).filter((i) => i.suspended).length;
  if (suspended) return badge(t("Needs attention"), { tone: "amber" });
  return badge(t("Connected"), { tone: "green" });
}

/** One provider row: tag, canonical connection status, account count, verbs. */
function providerRow(row, { admin, reload }) {
  const conn = row.connection || {};
  const runtime = row.runtime || {};
  const enabled = runtime.enabled !== false;
  const meta = connectionMeta(conn.status);
  const accountsText = conn.accounts_total
    ? t("{a} of {n} can take work", { a: conn.accounts_available ?? 0, n: conn.accounts_total })
    : t("No logins yet");
  return h(
    "tr",
    { "data-testid": `provider-${row.provider}` },
    h(
      "td",
      null,
      h(
        "div",
        { class: "cell-title" },
        providerTag(row.provider),
        row.support === "stable" ? null : badge(t("Experimental"), { tone: "amber" }),
      ),
      h("span", { class: "cell-sub" }, row.summary || ""),
    ),
    h(
      "td",
      null,
      enabled
        ? badge(meta.label, { tone: meta.tone, testid: `provider-status-${row.provider}` })
        : badge(t("Not enabled"), { tone: "neutral", title: t("Not selected by this deployment"), testid: `provider-status-${row.provider}` }),
    ),
    h("td", { class: "muted nowrap" }, enabled ? accountsText : t("Enable at deploy time (SBX_PROVIDERS)")),
    h(
      "td",
      { class: "num nowrap" },
      enabled && conn.status === "not_connected"
        ? admin
          ? button(t("Connect"), {
              size: "sm",
              variant: "primary",
              iconName: "link",
              testid: `connect-${row.provider}`,
              onClick: () => connectDialog(reload, { provider: row.provider }),
            })
          : h("span", { class: "subtle", title: t("Requires an API key with the admin scope") }, icon("lock", { size: 13, className: "muted-icon" }), " ", t("Admin"))
        : null,
      enabled && (conn.status === "connected" || conn.status === "degraded")
        ? h("a", { class: "btn btn-ghost btn-sm", href: "#/integrations/accounts", "data-testid": `manage-${row.provider}` }, icon("settings", { size: 14 }), h("span", null, t("Manage")))
        : null,
    ),
  );
}

export function renderIntegrations() {
  const admin = hasScope("admin");
  const githubStatus = h("span", { class: "hub-card-status" }, h("span", { class: "subtle" }, t("Checking…")));
  const providersBody = h("div", null, h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(4))));
  const attentionEl = h("div");

  async function load() {
    const issues = [];
    try {
      const res = await api.listProviders();
      const rows = res.providers || [];
      const degraded = rows.filter((r) => r.runtime?.enabled !== false && r.connection?.status === "degraded");
      issues.push(...degraded.map((r) => providerLabel(r.provider)));
      mount(
        providersBody,
        h(
          "div",
          { class: "table-wrap", style: "border:0" },
          labelize(
            h(
              "table",
              { class: "table", "data-testid": "providers-table" },
              h("thead", null, h("tr", null, [t("Provider"), t("Status"), t("Accounts"), ""].map((c) => h("th", null, c)))),
              h("tbody", null, rows.map((r) => providerRow(r, { admin, reload: load }))),
            ),
          ),
        ),
      );
    } catch (err) {
      mount(providersBody, errorBanner(err, { retry: load }));
    }
    let githubState = null;
    try {
      githubState = await api.githubStatus();
      if (githubStatus.isConnected) mount(githubStatus, githubBadge(githubState));
      if (githubState?.configured && githubState.broker?.url && !githubState.broker.healthy) issues.push(t("GitHub broker unreachable"));
      if (githubState?.installations?.some((i) => i.suspended)) issues.push(t("GitHub installation suspended"));
    } catch {
      if (githubStatus.isConnected) mount(githubStatus, h("span", { class: "subtle" }, t("Unavailable")));
    }
    mount(
      attentionEl,
      issues.length
        ? banner({
            tone: "warning",
            title: t("Needs attention"),
            body: issues.join(" · "),
            testid: "integrations-attention",
          })
        : null,
    );
  }

  const el = h(
    "div",
    { class: "page page-narrow", "data-testid": "integrations-view" },
    pageHeader({
      title: t("Integrations"),
      subtitle: t("Connect the AI providers and code hosts your agents run on."),
      testid: "page-title",
    }),
    attentionEl,
    card({
      title: t("GitHub"),
      iconName: "github",
      subtitle: t("Private repositories, branch push and pull-request delivery."),
      testid: "int-github-card",
      body: h(
        "div",
        { class: "row", style: "justify-content:space-between" },
        githubStatus,
        h("a", { class: "btn", href: "#/integrations/github", "data-testid": "int-github" }, t("Manage GitHub")),
      ),
    }),
    card({
      title: t("AI providers"),
      iconName: "users",
      subtitle: t("Agents run under your own provider subscriptions. Connect signs in with the provider's official login — no token paste."),
      testid: "int-providers",
      body: providersBody,
      footer: [
        h("a", { class: "btn btn-ghost btn-sm", href: "#/integrations/accounts", "data-testid": "int-accounts" }, icon("settings", { size: 14 }), h("span", null, t("Manage accounts"))),
        h("a", { class: "btn btn-ghost btn-sm", href: "#/settings/capacity", "data-testid": "int-capacity" }, icon("gauge", { size: 14 }), h("span", null, t("Capacity details"))),
      ],
    }),
  );

  void load();
  return { el, title: t("Integrations") };
}

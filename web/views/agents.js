import { api } from "../lib/api.js";
import { h, mount } from "../lib/dom.js";
import { isAgentEnded, PROVIDERS, providerLabel } from "../lib/domain.js";
import { fmtCompact, fmtRelative, fmtUsd, totalTokens } from "../lib/format.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { href, navigate } from "../lib/router.js";
import {
  badge,
  codeBlock,
  emptyState,
  errorBanner,
  linkButton,
  mono,
  pageHeader,
  poller,
  providerTag,
  segmented,
  skeleton,
  statusBadge,
} from "../lib/ui.js";

const FILTERS = [
  { value: "all", label: "All" },
  { value: "active", label: "Active" },
  { value: "running", label: "Running" },
  { value: "idle", label: "Idle" },
  { value: "ended", label: "Ended" },
];

function matchesFilter(agent, filter) {
  if (filter === "active") return !isAgentEnded(agent.status);
  if (filter === "running") return agent.status === "running" || agent.status === "creating";
  if (filter === "idle") return agent.status === "idle";
  if (filter === "ended") return isAgentEnded(agent.status);
  return true;
}

export function workflowChip(metadata) {
  if (!metadata?.workflow_id) return null;
  return h(
    "a",
    {
      class: "workflow-chip",
      href: href(`/workflows/${encodeURIComponent(metadata.workflow_id)}`),
      title: `${metadata.workflow_id} · ${metadata.role} · ${metadata.task_id}`,
      onClick: (ev) => ev.stopPropagation(),
    },
    icon("workflow", { size: 12 }),
    h("span", null, metadata.workflow_id),
    h("span", { class: "subtle" }, `· ${metadata.role}`),
  );
}

export function renderAgents({ route }) {
  const state = {
    agents: [],
    nextCursor: null,
    loading: true,
    error: null,
    filter: route.query.filter || "all",
    provider: route.query.provider || "",
    workflow: route.query.workflow || "",
    search: "",
  };

  const statsEl = h("div", { class: "stats", "data-testid": "agent-stats" });
  const listEl = h("div");

  const summaryQuery = () => ({
    provider: state.provider || undefined,
    workflow_id: state.workflow || undefined,
  });
  // SOR-202: `stamp` baselines the rollup version after each full page;
  // `hot` (live agents present) picks the summary tick cadence.
  let stamp = null;
  let hot = false;

  const syncQuery = () =>
    navigate("/agents", {
      filter: state.filter !== "all" ? state.filter : null,
      provider: state.provider || null,
      workflow: state.workflow || null,
    }, { silent: true });

  async function load({ append = false } = {}) {
    try {
      const res = await api.listAgents({
        provider: state.provider || undefined,
        workflow_id: state.workflow || undefined,
        cursor: append ? state.nextCursor : undefined,
      });
      state.agents = append ? [...state.agents, ...(res.agents || [])] : res.agents || [];
      state.nextCursor = res.next_cursor || null;
      state.error = null;
      try {
        const sum = await api.agentsSummary(summaryQuery());
        stamp = sum?.version || stamp;
        hot = Boolean(sum?.live);
      } catch {
        // Baseline stays; the next tick re-baselines.
      }
    } catch (err) {
      state.error = err;
    } finally {
      state.loading = false;
      render();
    }
  }

  function renderStats() {
    const all = state.agents;
    const count = (fn) => all.filter(fn).length;
    const cost = all.reduce((sum, a) => sum + (Number(a.cost_estimate_usd) || 0), 0);
    const stat = (label, value, sub, iconName) =>
      h("div", { class: "stat" }, h("span", { class: "stat-label" }, icon(iconName, { size: 14 }), label), h("span", { class: "stat-value" }, value), sub ? h("span", { class: "stat-sub" }, sub) : null);
    mount(
      statsEl,
      stat(t("Running"), String(count((a) => a.status === "running" || a.status === "creating")), t("working right now"), "zap"),
      stat(t("Idle"), String(count((a) => a.status === "idle")), t("ready for a follow-up"), "clock"),
      stat(t("Ended"), String(count((a) => isAgentEnded(a.status))), t("closed, timed out or lost"), "ban"),
      stat(t("Estimated cost"), fmtUsd(cost), t("sandbox compute, list price"), "cpu"),
    );
  }

  function renderList() {
    if (state.loading) return mount(listEl, h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(5))));
    if (state.error) return mount(listEl, errorBanner(state.error, { retry: () => load() }));
    const q = state.search.trim().toLowerCase();
    const rows = state.agents.filter(
      (a) => matchesFilter(a, state.filter) && (!q || `${a.name} ${a.id} ${a.model}`.toLowerCase().includes(q)),
    );
    if (!state.agents.length) {
      return mount(
        listEl,
        emptyState({
          iconName: "bot",
          title: t("No agents yet"),
          body: t("An agent is an isolated sandbox running a provider CLI. Create one from here or with a single API call."),
          actions: [linkButton(t("New agent"), "#/agents/new", { variant: "primary", iconName: "plus", testid: "empty-new-agent" })],
          testid: "agents-empty",
        }),
        h(
          "div",
          { style: "margin-top:16px" },
          codeBlock(
            `curl -X POST "$SBX_BASE_URL/v1/agents" \\\n  -H "Authorization: Bearer $SBX_API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '{"prompt":{"text":"Write hello.txt"},"agent":{"provider":"codex"}}'`,
            { lang: "bash" },
          ),
        ),
      );
    }
    if (!rows.length) {
      return mount(listEl, emptyState({ iconName: "search", title: t("No agents match these filters"), compact: true }));
    }
    const table = h(
      "table",
      { class: "table", "data-testid": "agents-table" },
      h(
        "thead",
        null,
        h("tr", null, [t("Agent"), t("Status"), t("Provider · model"), t("Workflow"), t("Tokens"), t("Cost"), t("Updated")].map((c, i) => h("th", { class: i >= 4 && i <= 5 ? "num" : null }, c))),
      ),
      h(
        "tbody",
        null,
        rows.map((a) =>
          h(
            "tr",
            {
              class: "is-link",
              "data-testid": "agent-row",
              "data-id": a.id,
              onClick: (ev) => {
                if (ev.target.closest("a,button")) return;
                navigate(`/agents/${encodeURIComponent(a.id)}`);
              },
            },
            h(
              "td",
              null,
              h(
                "div",
                { class: "cell-title" },
                h("a", { href: href(`/agents/${encodeURIComponent(a.id)}`), class: "agent-link" }, h("strong", null, a.name || a.id)),
                h("span", { class: "cell-sub" }, mono(a.id, { short: 18 })),
              ),
            ),
            h("td", null, statusBadge("agent", a.status)),
            h(
              "td",
              null,
              h(
                "div",
                { class: "cell-title" },
                providerTag(a.provider),
                h("span", { class: "cell-sub" }, [a.model, a.reasoning_effort ? `${t("effort")} ${a.reasoning_effort}` : null].filter(Boolean).join(" · ")),
              ),
            ),
            h("td", null, workflowChip(a.metadata) || h("span", { class: "subtle" }, "—")),
            h("td", { class: "num", title: a.usage ? JSON.stringify(a.usage) : t("Not measured yet") }, a.usage ? fmtCompact(totalTokens(a.usage)) : h("span", { class: "subtle" }, "—")),
            h("td", { class: "num" }, fmtUsd(a.cost_estimate_usd)),
            h("td", { class: "nowrap muted", title: a.updated_at }, fmtRelative(a.updated_at)),
          ),
        ),
      ),
    );
    mount(
      listEl,
      h("div", { class: "table-wrap" }, table),
      state.nextCursor
        ? h("div", { style: "display:flex;justify-content:center;margin-top:12px" }, h("button", { class: "btn btn-sm", type: "button", onClick: () => load({ append: true }) }, t("Load more")))
        : null,
    );
  }

  function render() {
    renderStats();
    renderList();
  }

  const searchInput = h("input", {
    class: "input",
    type: "search",
    placeholder: t("Search by name, id or model"),
    "data-testid": "agents-search",
    onInput: (ev) => {
      state.search = ev.target.value;
      renderList();
    },
  });
  const providerSelect = h(
    "select",
    {
      class: "select",
      "data-testid": "agents-provider",
      "aria-label": t("Provider"),
      onChange: (ev) => {
        state.provider = ev.target.value;
        syncQuery();
        state.loading = true;
        render();
        void load();
      },
    },
    h("option", { value: "" }, t("All providers")),
    PROVIDERS.map((p) => h("option", { value: p, selected: p === state.provider }, providerLabel(p))),
  );
  const workflowInput = h("input", {
    class: "input",
    type: "search",
    placeholder: t("Workflow id"),
    value: state.workflow,
    style: "width:180px",
    "data-testid": "agents-workflow",
    onChange: (ev) => {
      state.workflow = ev.target.value.trim();
      syncQuery();
      state.loading = true;
      render();
      void load();
    },
  });

  const el = h(
    "div",
    { class: "page page-wide" },
    pageHeader({
      title: t("Agents"),
      subtitle: t("Each agent is one isolated Modal Sandbox running an official provider CLI. Runs are turns of work on it."),
      actions: [linkButton(t("New agent"), "#/agents/new", { variant: "primary", iconName: "plus", testid: "new-agent" })],
      testid: "page-title",
    }),
    statsEl,
    h(
      "div",
      { class: "toolbar" },
      segmented(
        FILTERS.map((f) => ({ ...f, label: t(f.label) })),
        state.filter,
        (v) => {
          state.filter = v;
          syncQuery();
          renderList();
        },
        { testid: "agents-filter" },
      ),
      h("div", { class: "grow input-affix" }, icon("search", { size: 14 }), searchInput),
      providerSelect,
      workflowInput,
    ),
    listEl,
  );

  if (state.workflow) {
    el.insertBefore(h("div", { style: "margin-bottom:12px" }, badge(`${t("Workflow")}: ${state.workflow}`, { tone: "accent" })), listEl);
  }

  render();
  void load();
  // Cheap summary tick (SOR-202): the full listAgents page is only
  // refetched when the rollup's version moved — never on a fixed cadence.
  const poll = poller(async () => {
    let sum = null;
    try {
      sum = await api.agentsSummary(summaryQuery());
    } catch {
      return; // transient failure — keep the current cadence
    }
    hot = Boolean(sum?.live);
    const version = sum?.version || "";
    if (stamp !== null && version !== stamp) await load();
    stamp = version;
  }, () => (hot ? 5000 : 30000));
  return { el, title: t("Agents"), dispose: () => poll.stop() };
}

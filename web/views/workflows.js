import { api } from "../lib/api.js";
import { h, mount } from "../lib/dom.js";
import { fmtRelative } from "../lib/format.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { href, navigate } from "../lib/router.js";
import { recentWorkflows } from "../lib/store.js";
import {
  actionButton,
  badge,
  card,
  confirmDialog,
  emptyState,
  errorBanner,
  kv,
  labelize,
  openDialog,
  pageHeader,
  poller,
  providerTag,
  skeleton,
  statusBadge,
  toast,
  toastError,
} from "../lib/ui.js";

const RUN_TONES = {
  FINISHED: "var(--tone-green-dot)",
  RUNNING: "var(--tone-blue-dot)",
  CREATING: "var(--tone-violet-dot)",
  ERROR: "var(--tone-red-dot)",
  EXPIRED: "var(--tone-amber-dot)",
  CANCELLED: "var(--tone-neutral-dot)",
  UNKNOWN: "var(--border-strong)",
};

export function renderWorkflows() {
  const listEl = h("div", null, skeleton(3));
  const input = h("input", { class: "input mono", placeholder: "release-42", "data-testid": "workflow-lookup", required: true });

  async function load() {
    let fromAgents = [];
    try {
      const res = await api.listAgents();
      const byId = new Map();
      for (const a of res.agents || []) {
        const wid = a.metadata?.workflow_id;
        if (!wid) continue;
        const entry = byId.get(wid) || { id: wid, agents: 0, open: 0, updated: a.updated_at };
        entry.agents += 1;
        if (["creating", "idle", "running"].includes(a.status)) entry.open += 1;
        if ((a.updated_at || "") > (entry.updated || "")) entry.updated = a.updated_at;
        byId.set(wid, entry);
      }
      fromAgents = [...byId.values()].sort((a, b) => (b.updated || "").localeCompare(a.updated || ""));
    } catch (err) {
      mount(listEl, errorBanner(err, { retry: load }));
      return;
    }
    const known = new Set(fromAgents.map((w) => w.id));
    const recent = recentWorkflows.list().filter((id) => !known.has(id));
    if (!fromAgents.length && !recent.length) {
      mount(
        listEl,
        emptyState({
          iconName: "workflow",
          title: t("No workflows yet"),
          body: t("Attach `metadata: {workflow_id, task_id, role}` when creating agents. Any client holding the same API key can then recover the whole workflow — agents, latest runs and artifacts — from its id alone."),
          testid: "workflows-empty",
        }),
      );
      return;
    }
    mount(
      listEl,
      h(
        "div",
        { class: "table-wrap" },
        labelize(
          h(
          "table",
          { class: "table", "data-testid": "workflows-table" },
          h("thead", null, h("tr", null, [t("Workflow"), t("Agents"), t("Open"), t("Last activity")].map((c) => h("th", null, c)))),
          h(
            "tbody",
            null,
            fromAgents.map((w) =>
              h(
                "tr",
                { class: "is-link", onClick: () => navigate(`/workflows/${encodeURIComponent(w.id)}`) },
                h("td", null, h("a", { href: href(`/workflows/${encodeURIComponent(w.id)}`), class: "mono" }, w.id)),
                h("td", null, String(w.agents)),
                h("td", null, w.open ? badge(String(w.open), { tone: "blue" }) : h("span", { class: "subtle" }, "0")),
                h("td", { class: "muted" }, fmtRelative(w.updated)),
              ),
            ),
            recent.map((id) =>
              h(
                "tr",
                { class: "is-link", onClick: () => navigate(`/workflows/${encodeURIComponent(id)}`) },
                h("td", null, h("a", { href: href(`/workflows/${encodeURIComponent(id)}`), class: "mono" }, id)),
                h("td", { colspan: "3", class: "subtle" }, t("Recently viewed")),
              ),
            ),
          ),
          ),
        ),
      ),
    );
  }

  const el = h(
    "div",
    { class: "page" },
    pageHeader({
      title: t("Workflows"),
      subtitle: t("Groups of agents bound by a workflow id — recover their state from any process, or clean them up in one call."),
      testid: "page-title",
    }),
    card({
      title: t("Recover a workflow"),
      subtitle: t("Looks up agents bound to this id under your API key. Served from durable records only."),
      iconName: "search",
      body: h(
        "form",
        {
          class: "input-group",
          onSubmit: (ev) => {
            ev.preventDefault();
            const id = input.value.trim();
            if (id) navigate(`/workflows/${encodeURIComponent(id)}`);
          },
        },
        input,
        h("button", { class: "btn btn-primary", type: "submit" }, t("Open")),
      ),
    }),
    h("div", { style: "margin-top:20px" }, listEl),
  );
  void load();
  return { el, title: t("Workflows") };
}

export function renderWorkflow({ route }) {
  const id = route.params.id;
  const body = h("div", null, h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(6))));
  const actionsEl = h("div", { class: "page-actions" });
  let data = null;

  function progressCard(wf) {
    const p = wf.progress;
    const byStatus = Object.entries(p.latest_runs_by_status || {});
    const total = byStatus.reduce((n, [, c]) => n + c, 0) || 1;
    const stat = (label, value) => h("div", { class: "stat" }, h("span", { class: "stat-label" }, label), h("span", { class: "stat-value" }, String(value)));
    return card({
      title: t("Progress"),
      iconName: "gauge",
      actions: p.all_terminal ? badge(t("All runs finished"), { tone: "green" }) : badge(t("In progress"), { tone: "blue" }),
      body: h(
        "div",
        { class: "stack" },
        h("div", { class: "stats", style: "margin:0" }, stat(t("Tasks"), p.tasks), stat(t("Agents"), p.agents), stat(t("Open agents"), p.open_agents), stat(t("Runs"), p.runs)),
        byStatus.length
          ? h(
              "div",
              { class: "stack", style: "gap:8px" },
              h("div", { class: "stacked" }, byStatus.map(([s, c]) => h("span", { style: { width: `${(c / total) * 100}%`, background: RUN_TONES[s] || "var(--text-3)" }, title: `${s}: ${c}` }))),
              h("div", { class: "legend" }, byStatus.map(([s, c]) => h("span", null, h("i", { style: { background: RUN_TONES[s] || "var(--text-3)" } }), `${s} · ${c}`))),
            )
          : null,
      ),
    });
  }

  function depth(agent, byTask) {
    let d = 0;
    let cur = agent;
    const seen = new Set();
    while (cur?.parent_task_id && byTask.has(cur.parent_task_id) && !seen.has(cur.parent_task_id) && d < 6) {
      seen.add(cur.parent_task_id);
      cur = byTask.get(cur.parent_task_id);
      d += 1;
    }
    return d;
  }

  function agentsCard(wf) {
    const byTask = new Map(wf.agents.map((a) => [a.task_id, a]));
    return card({
      title: t("Tasks"),
      subtitle: t("Indented tasks are children of the task above (parent_task_id)."),
      iconName: "bot",
      body: h(
        "div",
        { class: "table-wrap", style: "border:0" },
        labelize(
          h(
          "table",
          { class: "table", "data-testid": "workflow-agents" },
          h("thead", null, h("tr", null, [t("Task"), t("Role"), t("Agent"), t("Status"), t("Latest run"), t("Runs")].map((c) => h("th", null, c)))),
          h(
            "tbody",
            null,
            wf.agents.map((a) =>
              h(
                "tr",
                null,
                h("td", null, h("span", { style: { paddingLeft: `${depth(a, byTask) * 18}px` }, class: "row" }, depth(a, byTask) ? icon("chevronRight", { size: 12, className: "muted-icon" }) : null, h("code", null, a.task_id))),
                h("td", null, badge(a.role)),
                h("td", null, h("div", { class: "cell-title" }, h("a", { href: href(`/agents/${encodeURIComponent(a.agent_id)}`), class: "mono" }, a.agent_id), a.provider ? h("span", { class: "cell-sub" }, providerTag(a.provider), " ", a.model || "") : null)),
                h("td", null, statusBadge("agent", a.status)),
                h(
                  "td",
                  null,
                  a.latest_run
                    ? h("div", { class: "cell-title" }, statusBadge("run", a.latest_run.status), a.latest_run.result?.text ? h("span", { class: "cell-sub", title: a.latest_run.result.text }, a.latest_run.result.text.slice(0, 80)) : null)
                    : h("span", { class: "subtle" }, "—"),
                ),
                h("td", null, String(a.runs)),
              ),
            ),
          ),
          ),
        ),
      ),
    });
  }

  async function load() {
    try {
      data = await api.workflow(id);
      recentWorkflows.add(id);
    } catch (err) {
      mount(
        body,
        err.status === 404
          ? emptyState({ iconName: "search", title: t("No agents are bound to this workflow"), body: t("Workflow ids are scoped to your API key. Check the id, or create an agent with this workflow id."), testid: "workflow-missing" })
          : errorBanner(err, { retry: load }),
      );
      mount(actionsEl);
      return;
    }
    mount(
      actionsEl,
      h("a", { class: "btn", href: href("/agents/new", { workflow_id: id }) }, icon("plus"), t("Add task")),
      h("a", { class: "btn", href: href("/agents", { workflow: id }) }, icon("bot"), t("View agents")),
      data.progress.open_agents
        ? actionButton(t("Close workflow"), async () => {
            const ok = await confirmDialog({
              title: t("Close every agent in this workflow?"),
              body: t("Only agents bound to this workflow under your key are closed. Run history and artifacts stay available. This is safe to repeat."),
              confirmLabel: t("Close workflow"),
            });
            if (!ok) return;
            try {
              const res = await api.closeWorkflow(id);
              showCleanup(res);
              await load();
            } catch (err) {
              toastError(err, t("Cleanup failed"));
            }
          }, { variant: "danger", iconName: "x", testid: "close-workflow" })
        : null,
    );
    mount(body, h("div", { class: "stack" }, progressCard(data), agentsCard(data)));
  }

  function showCleanup(res) {
    toast(t("Closed {n} agent(s)", { n: res.closed.length }), { tone: "success" });
    const row = (label, list) => [label, list?.length ? h("span", { class: "row", style: "gap:4px" }, list.map((x) => h("code", null, x))) : null];
    openDialog({
      title: t("Workflow cleanup"),
      size: "sm",
      body: kv([
        [t("Matched"), String(res.matched)],
        row(t("Closed"), res.closed),
        row(t("Already ended"), res.already_terminal),
        row(t("Missing"), res.missing),
        row(t("Skipped (other owner)"), res.skipped),
        [t("Errors"), Object.keys(res.errors || {}).length ? h("code", null, JSON.stringify(res.errors)) : null],
      ]),
    });
  }

  const el = h(
    "div",
    { class: "page page-wide" },
    h(
      "header",
      { class: "page-header" },
      h("a", { class: "back-link", href: "#/workflows" }, icon("arrowLeft", { size: 14 }), t("Workflows")),
      h("div", { class: "page-header-row" }, h("div", { class: "page-title" }, h("h1", { class: "mono", "data-testid": "workflow-title" }, id), h("p", { class: "muted" }, t("Recovery view — everything a fresh client needs to resume this workflow."))), actionsEl),
    ),
    body,
  );
  void load();
  const poll = poller(load, 6000);
  return { el, title: id, dispose: () => poll.stop() };
}

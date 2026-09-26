import { api } from "../lib/api.js";
import { h, mount } from "../lib/dom.js";
import { isTaskAttention, isTaskLive, providerLabel, repoName, taskTitle } from "../lib/domain.js";
import { fmtDateTime, fmtRelative } from "../lib/format.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { href, navigate } from "../lib/router.js";
import { emptyState, errorBanner, labelize, linkButton, pageHeader, poller, skeleton, statusBadge } from "../lib/ui.js";

/** Task row shared by Home and Tasks: title + status + ai + repo + updated. */
export function taskRow(task, { compact } = {}) {
  const ex = task.resolved?.execution || task.request?.execution || {};
  const repo = task.resolved?.source?.repo || task.request?.source?.repo || "";
  // The subtitle is context for a named task; for unnamed tasks the title is
  // already the prompt's first line, so repeating it adds noise.
  const name = task.request?.name?.trim();
  const promptLine = String(task.prompt?.text || "").split("\n")[0].slice(0, 90);
  const sub = name && promptLine && promptLine !== name ? promptLine : null;
  return h(
    "tr",
    {
      class: "is-link",
      "data-testid": "task-row",
      "data-id": task.id,
      onClick: (ev) => {
        if (ev.target.closest("a,button")) return;
        navigate(`/tasks/${encodeURIComponent(task.id)}`);
      },
    },
    h(
      "td",
      null,
      h(
        "div",
        { class: "cell-title" },
        h("a", { href: href(`/tasks/${encodeURIComponent(task.id)}`), class: "agent-link" }, h("strong", null, taskTitle(task))),
        sub ? h("span", { class: "cell-sub" }, sub) : null,
      ),
    ),
    h("td", null, statusBadge("task", task.status)),
    h(
      "td",
      null,
      h(
        "div",
        { class: "cell-title" },
        h("span", null, ex.provider && ex.provider !== "auto" ? providerLabel(ex.provider) : t("Automatic")),
        h("span", { class: "cell-sub" }, [ex.model && ex.model !== "auto" ? ex.model : null, ex.reasoning_effort && ex.reasoning_effort !== "auto" ? `${t("effort")} ${ex.reasoning_effort}` : null].filter(Boolean).join(" · ")),
      ),
    ),
    compact ? null : h("td", { class: "col-repo" }, repo ? h("span", { class: "mono-sm", title: repo }, repoName(repo)) : h("span", { class: "subtle" }, "—")),
    h("td", { class: "nowrap muted", title: fmtDateTime(task.updated_at) }, fmtRelative(task.updated_at)),
  );
}

export function renderHome() {
  const statsEl = h("div", { class: "stats", "data-testid": "home-stats" });
  const listEl = h("div");

  const stat = (label, value, sub, iconName, testid) =>
    h(
      "div",
      { class: "stat", "data-testid": testid },
      h("span", { class: "stat-label" }, icon(iconName, { size: 14 }), label),
      h("span", { class: "stat-value" }, value),
      sub ? h("span", { class: "stat-sub" }, sub) : null,
    );

  function render(tasks) {
    const live = tasks.filter((x) => isTaskLive(x.status));
    const attention = tasks.filter((x) => isTaskAttention(x.status));
    const finished = tasks.filter((x) => x.status === "finished");
    mount(
      statsEl,
      stat(t("Active"), String(live.length), t("queued or running"), "zap", "home-stat-active"),
      stat(t("Finished"), String(finished.length), t("completed successfully"), "circleCheck", "home-stat-finished"),
      stat(t("Needs attention"), String(attention.length), t("failed, expired or undelivered"), "alert", "home-stat-attention"),
      stat(t("Total"), String(tasks.length), t("on this API key"), "listTodo"),
    );
    if (!tasks.length) {
      mount(
        listEl,
        h(
          "div",
          { class: "card" },
          h(
            "div",
            { class: "card-body" },
            emptyState({
              iconName: "listTodo",
              title: t("No tasks yet"),
              body: t("Describe what you want done — the console picks an agent, resolves the repository and tracks the result."),
              actions: [linkButton(t("Create your first task"), "#/tasks/new", { variant: "primary", iconName: "plus", testid: "home-new-task-empty" })],
              testid: "home-empty",
            }),
          ),
        ),
      );
      return;
    }
    const recent = tasks
      .slice()
      .sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""))
      .slice(0, 8);
    mount(
      listEl,
      h(
        "div",
        { class: "table-wrap" },
        labelize(
          h(
            "table",
            { class: "table", "data-testid": "home-tasks" },
            h("thead", null, h("tr", null, [t("Task"), t("Status"), t("AI"), t("Repository"), t("Updated")].map((c) => h("th", null, c)))),
            h("tbody", null, recent.map((task) => taskRow(task))),
          ),
        ),
      ),
    );
  }

  let hot = false;
  async function load() {
    try {
      const res = await api.listTasks();
      const tasks = res.tasks || [];
      hot = tasks.some((x) => isTaskLive(x.status));
      render(tasks);
    } catch (err) {
      mount(listEl, errorBanner(err, { retry: () => void load() }));
    }
  }

  const el = h(
    "div",
    { class: "page", "data-testid": "home-view" },
    pageHeader({
      title: t("Home"),
      subtitle: t("Describe a task, let the agent do it, review the result."),
      actions: [linkButton(t("New task"), "#/tasks/new", { variant: "primary", iconName: "plus", testid: "home-new-task" })],
      testid: "page-title",
    }),
    statsEl,
    h("h2", { class: "section-title" }, t("Recent tasks")),
    listEl,
  );

  mount(listEl, h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(4))));
  void load();
  const poll = poller(() => void load(), () => (hot ? 5000 : 30000));
  return { el, title: t("Home"), dispose: () => poll.stop() };
}

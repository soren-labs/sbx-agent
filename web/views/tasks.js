import { api } from "../lib/api.js";
import { h, mount } from "../lib/dom.js";
import { isTaskAttention, isTaskLive, taskTitle } from "../lib/domain.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { navigate } from "../lib/router.js";
import { emptyState, errorBanner, linkButton, pageHeader, poller, segmented, skeleton } from "../lib/ui.js";
import { taskRow } from "./home.js";

const FILTERS = [
  { value: "all", label: "All" },
  { value: "active", label: "Active" },
  { value: "finished", label: "Finished" },
  { value: "attention", label: "Needs attention" },
  { value: "cancelled", label: "Cancelled" },
];

function matches(task, filter) {
  if (filter === "active") return isTaskLive(task.status);
  if (filter === "finished") return task.status === "finished";
  if (filter === "attention") return isTaskAttention(task.status);
  if (filter === "cancelled") return ["cancelled", "expired"].includes(task.status);
  return true;
}

export function renderTasks({ route }) {
  const state = { tasks: [], loading: true, error: null, filter: route.query.filter || "all", search: "" };
  const listEl = h("div");

  const syncQuery = () =>
    navigate("/tasks", { filter: state.filter !== "all" ? state.filter : null }, { silent: true });

  function matchesSearch(task, q) {
    if (!q) return true;
    const repo = task.resolved?.source?.repo || task.request?.source?.repo || "";
    const ex = task.resolved?.execution || {};
    return `${taskTitle(task)} ${task.id} ${task.prompt?.text || ""} ${repo} ${ex.provider || ""} ${ex.model || ""}`
      .toLowerCase()
      .includes(q);
  }

  function render() {
    if (state.loading) return mount(listEl, h("div", { class: "card" }, h("div", { class: "card-body" }, skeleton(5))));
    if (state.error) return mount(listEl, errorBanner(state.error, { retry: () => load() }));
    if (!state.tasks.length) {
      return mount(
        listEl,
        emptyState({
          iconName: "listTodo",
          title: t("No tasks yet"),
          body: t("A task is a piece of work for an agent — describe the goal, pick a repository if it changes code, and get a finished result back."),
          actions: [linkButton(t("New task"), "#/tasks/new", { variant: "primary", iconName: "plus", testid: "tasks-new-empty" })],
          testid: "tasks-empty",
        }),
      );
    }
    const q = state.search.trim().toLowerCase();
    const rows = state.tasks
      .filter((task) => matches(task, state.filter) && matchesSearch(task, q))
      .sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    if (!rows.length) {
      return mount(listEl, emptyState({ iconName: "search", title: t("No tasks match these filters"), compact: true, testid: "tasks-none" }));
    }
    mount(
      listEl,
      h(
        "div",
        { class: "table-wrap" },
        h(
          "table",
          { class: "table", "data-testid": "tasks-table" },
          h("thead", null, h("tr", null, [t("Task"), t("Status"), t("AI"), t("Repository"), t("Updated")].map((c) => h("th", null, c)))),
          h("tbody", null, rows.map((task) => taskRow(task))),
        ),
      ),
    );
  }

  let hot = false;
  async function load() {
    try {
      const res = await api.listTasks();
      state.tasks = res.tasks || [];
      state.error = null;
      hot = state.tasks.some((task) => isTaskLive(task.status));
    } catch (err) {
      state.error = err;
    } finally {
      state.loading = false;
      render();
    }
  }

  const searchInput = h("input", {
    class: "input",
    type: "search",
    placeholder: t("Search by name, prompt or repo"),
    "data-testid": "tasks-search",
    onInput: (ev) => {
      state.search = ev.target.value;
      render();
    },
  });

  const el = h(
    "div",
    { class: "page page-wide", "data-testid": "tasks-view" },
    pageHeader({
      title: t("Tasks"),
      subtitle: t("Each task runs on its own sandboxed agent and reports a result — an answer, a branch or a pull request."),
      actions: [linkButton(t("New task"), "#/tasks/new", { variant: "primary", iconName: "plus", testid: "new-task" })],
      testid: "page-title",
    }),
    h(
      "div",
      { class: "toolbar" },
      segmented(
        FILTERS.map((f) => ({ ...f, label: t(f.label) })),
        state.filter,
        (v) => {
          state.filter = v;
          syncQuery();
          render();
        },
        { testid: "tasks-filter" },
      ),
      h("div", { class: "grow input-affix" }, icon("search", { size: 14 }), searchInput),
    ),
    listEl,
  );

  render();
  void load();
  const poll = poller(() => void load(), () => (hot ? 5000 : 30000));
  return { el, title: t("Tasks"), dispose: () => poll.stop() };
}

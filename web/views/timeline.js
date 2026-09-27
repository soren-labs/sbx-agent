/**
 * One run in the agent conversation: prompt, streamed activity (canonical
 * events), final result from the durable ledger, error + contract verdict.
 * Items are keyed nodes so streaming updates never re-render the run.
 */
import { h, mount } from "../lib/dom.js";
import { explainRunError, isRunLive, providerLabel } from "../lib/domain.js";
import { fmtDateTime, fmtDuration, fmtNumber, fmtRelative, secondsBetween, shortSha } from "../lib/format.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";
import { renderMarkdown } from "../lib/markdown.js";
import { href } from "../lib/router.js";
import { prompts } from "../lib/store.js";
import { badge, banner, button, copyButton, jsonView, kv, statusBadge } from "../lib/ui.js";

export function runNumber(runId) {
  const m = String(runId).match(/(\d+)$/);
  return m ? Number(m[1]) : null;
}

/** `/bin/bash -lc "cmd"` → `cmd` for display. */
export function displayCommand(command) {
  if (Array.isArray(command)) return command.join(" ");
  const s = String(command || "");
  const m = s.match(/^\/(?:usr\/)?bin\/(?:ba|z)?sh\s+-l?c\s+(["'])([\s\S]*)\1$/);
  if (!m) return s;
  return m[1] === '"' ? m[2].replace(/\\(["\\$`])/g, "$1") : m[2];
}

function toolDetails({ key, kindIcon, kindLabel, summary, status, body, testid, open }) {
  const d = h(
    "details",
    { class: "tool", "data-key": key, "data-testid": testid, open: open || null },
    h(
      "summary",
      null,
      icon("chevronRight", { size: 14, className: "chev" }),
      h("span", { class: "tool-kind" }, icon(kindIcon, { size: 14 }), kindLabel),
      h("span", { class: "tool-summary", title: typeof summary === "string" ? summary : null }, summary),
      status,
    ),
    h("div", { class: "tool-body" }, body),
  );
  return d;
}

function itemStatus(item) {
  if (item.status === "in_progress") return h("span", { class: "tool-state", title: t("In progress") }, icon("loader", { size: 14, className: "spin" }));
  if (item.type === "command_execution" && item.exit_code != null) {
    return item.exit_code === 0
      ? h("span", { class: "tool-state is-ok", title: "exit 0" }, icon("check", { size: 14 }))
      : badge(`exit ${item.exit_code}`, { tone: "red", mono: true });
  }
  if (item.status === "failed") return badge(t("failed"), { tone: "red" });
  return null;
}

/** Tool-ish items fold into the run's work log; messages and errors stay inline. */
function isWorkItem(item) {
  return item.type !== "agent_message" && item.type !== "error";
}

function isFailedItem(item) {
  return item.status === "failed" || (item.type === "command_execution" && item.exit_code != null && item.exit_code !== 0);
}

function renderItem(item, key, provider, wasOpen) {
  switch (item.type) {
    case "agent_message":
      return h(
        "div",
        { class: "msg msg-agent", "data-key": key, "data-testid": "agent-message" },
        h("div", { class: "msg-head" }, icon("bot", { size: 14 }), providerLabel(provider) || t("Agent")),
        renderMarkdown(item.text || ""),
      );
    case "command_execution": {
      const cmd = displayCommand(item.command);
      return toolDetails({
        key,
        testid: "command-block",
        kindIcon: "terminal",
        kindLabel: t("Command"),
        summary: cmd,
        status: itemStatus(item),
        open: wasOpen,
        body: h(
          "div",
          { class: "terminal-wrap" },
          h(
            "pre",
            { class: "terminal", "data-testid": "command-output" },
            h("span", { class: "prompt" }, "$ "),
            cmd,
            item.aggregated_output ? `\n${item.aggregated_output}` : item.status === "in_progress" ? "" : `\n${t("(no output)")}`,
          ),
          h("div", { class: "terminal-actions" }, copyButton(() => `$ ${cmd}\n${item.aggregated_output || ""}`, { title: t("Copy command and output") })),
        ),
      });
    }
    case "file_change": {
      const changes = Array.isArray(item.changes) ? item.changes : [];
      const first = changes[0]?.path || "";
      return toolDetails({
        key,
        testid: "file-block",
        kindIcon: "fileDiff",
        kindLabel: t("Files"),
        summary: changes.length > 1 ? t("{path} and {n} more", { path: first, n: changes.length - 1 }) : first,
        status: itemStatus(item),
        open: wasOpen,
        body: h(
          "ul",
          { class: "file-list", "data-testid": "file-list" },
          changes.map((c) =>
            h("li", null, badge(c.kind || "change", { tone: c.kind === "add" ? "green" : c.kind === "delete" ? "red" : "blue" }), c.path),
          ),
        ),
      });
    }
    case "reasoning": {
      const text = item.text || "";
      return toolDetails({
        key,
        testid: "reasoning-block",
        kindIcon: "sparkles",
        kindLabel: t("Thinking"),
        summary: text.split("\n")[0].slice(0, 160),
        status: null,
        open: wasOpen,
        body: h("div", { class: "reasoning" }, text),
      });
    }
    case "error":
      return h(
        "div",
        { "data-key": key, "data-testid": "item-error" },
        banner({ tone: "warning", title: t("Provider reported an error"), body: item.message || "" }),
      );
    default:
      return toolDetails({
        key,
        testid: "item-other",
        kindIcon: "braces",
        kindLabel: item.type || t("Event"),
        summary: item.name || item.tool || item.server || "",
        status: itemStatus(item),
        open: wasOpen,
        body: jsonView(item),
      });
  }
}

function usageText(usage) {
  if (!usage) return null;
  const parts = [
    `${fmtNumber(usage.input_tokens)} ${t("in")}`,
    usage.cached_input_tokens ? `${fmtNumber(usage.cached_input_tokens)} ${t("cached")}` : null,
    `${fmtNumber(usage.output_tokens)} ${t("out")}`,
    usage.reasoning_output_tokens ? `${fmtNumber(usage.reasoning_output_tokens)} ${t("reasoning")}` : null,
  ];
  return parts.filter(Boolean).join(" · ");
}

function contractPanel(run) {
  const c = run.output_contract;
  if (!c) return null;
  const tone = { valid: "green", invalid: "red", pending: "blue", skipped: "neutral" }[c.status] || "neutral";
  return h(
    "div",
    { class: "contract", "data-testid": "contract-verdict" },
    h(
      "div",
      { class: "contract-head" },
      icon("braces", { size: 14 }),
      t("Output contract"),
      badge(t(c.status), { tone }),
      badge(c.enforcement, { mono: true }),
      c.extraction ? h("span", { class: "subtle" }, `${t("extracted from")} ${c.extraction}`) : null,
    ),
    c.violations?.length
      ? h(
          "table",
          { class: "table" },
          h("thead", null, h("tr", null, h("th", null, t("Path")), h("th", null, t("Rule")), h("th", null, t("Message")))),
          h("tbody", null, c.violations.map((v) => h("tr", null, h("td", null, h("code", null, v.path || "$")), h("td", null, h("code", null, v.code)), h("td", null, v.message)))),
        )
      : null,
    run.structured_output !== undefined && run.structured_output !== null ? jsonView(run.structured_output, { testid: "structured-output" }) : null,
  );
}

function errorPanel(run) {
  const e = run.error;
  if (!e) return null;
  const help = explainRunError(e);
  return h(
    "div",
    { class: "run-error", "data-testid": "run-error" },
    banner({
      tone: run.status === "CANCELLED" ? "neutral" : "danger",
      title: `${help.what || e.message}`,
      body: h(
        "div",
        { class: "stack", style: "gap:8px" },
        help.next ? h("span", null, help.next) : null,
        kv([
          [t("Code"), h("code", null, e.code)],
          [t("Source"), e.source],
          [t("Retryable"), e.retryable ? t("yes") : t("no")],
          e.retry_after != null ? [t("Retry after"), fmtDuration(e.retry_after)] : null,
          e.message && e.message !== help.what ? [t("Message"), h("span", { class: "mono" }, e.message)] : null,
        ]),
      ),
    }),
  );
}

export function createRunBlock({ agentId, provider, run, agentLive, expanded = true, onLoadActivity, onActivity }) {
  const state = {
    run,
    items: new Map(),
    events: [],
    threadId: null,
    finished: null,
    streamState: "idle",
    activityLoaded: false,
    agentLive,
    expanded,
  };
  // Consecutive work items (commands, file edits, thinking) share one
  // collapsible work log; an agent message closes the current log.
  const groups = [];
  let openGroup = null;
  const n = runNumber(run.id);

  const headerEl = h("div", { class: "run-divider" });
  const promptEl = h("div");
  const activityEl = h("div", { class: "activity", "data-testid": "run-activity" });
  const noticeEl = h("div", { class: "activity" });
  const resultEl = h("div");
  const workingEl = h("div");
  const tailEl = h("div", { class: "stack", style: "gap:10px" });
  const footEl = h("div", { class: "run-foot" });
  const rawEl = h("div");

  const el = h(
    "article",
    { class: "run", "data-testid": "run", "data-run": run.id, "data-status": run.status },
    headerEl,
    promptEl,
    activityEl,
    noticeEl,
    resultEl,
    workingEl,
    tailEl,
    footEl,
    rawEl,
  );

  let showRaw = false;
  let tick = null;

  function renderHeader() {
    const r = state.run;
    const dur = secondsBetween(r.started_at || r.created_at, r.finished_at);
    mount(
      headerEl,
      h("span", { class: "run-num" }, t("Run {n}", { n: n ?? r.id })),
      statusBadge("run", r.status, { testid: "run-status" }),
      h("span", { title: fmtDateTime(r.created_at) }, fmtRelative(r.created_at)),
      dur != null ? h("span", { class: "sep" }, "·") : null,
      dur != null ? h("span", { "data-testid": "run-duration" }, fmtDuration(dur)) : null,
      r.model ? h("span", { class: "sep" }, "·") : null,
      r.model ? h("span", { class: "mono" }, r.model) : null,
      r.reasoning_effort ? badge(`${t("effort")} ${r.reasoning_effort}`) : null,
      h(
        "span",
        { class: "row", style: "gap:2px" },
        copyButton(r.id, { title: t("Copy run id") }),
        button("", {
          variant: "ghost",
          size: "xs",
          iconName: "braces",
          title: t("Show raw run record and events"),
          onClick: () => {
            showRaw = !showRaw;
            renderRaw();
          },
        }),
      ),
    );
  }

  function renderPrompt() {
    const text = state.run.prompt?.text || prompts.get(agentId, state.run.id);
    mount(
      promptEl,
      text
        ? h("div", { class: "msg msg-user", "data-testid": "user-message" }, text)
        : h("div", { class: "msg-user-missing", title: t("This run predates prompt recording, or its prompt is no longer available.") }, t("Prompt unavailable")),
    );
  }

  function hasAgentMessage() {
    for (const { item } of state.items.values()) if (item.type === "agent_message" && item.text) return true;
    return false;
  }

  function renderResult() {
    const r = state.run;
    const text = r.result?.text;
    if (text && !hasAgentMessage()) {
      mount(
        resultEl,
        h(
          "div",
          { class: "msg msg-agent", "data-testid": "run-result" },
          h("div", { class: "msg-head" }, icon("bot", { size: 14 }), providerLabel(provider), h("span", { class: "subtle" }, `· ${t("final result")}`)),
          renderMarkdown(text),
        ),
      );
    } else {
      mount(resultEl);
    }
  }

  function renderWorking() {
    const r = state.run;
    if (!isRunLive(r.status)) {
      mount(workingEl);
      clearInterval(tick);
      tick = null;
      return;
    }
    const label =
      r.status === "CREATING" ? t("Starting sandbox…") : state.streamState === "reconnecting" ? t("Reconnecting to the stream…") : t("Working…");
    mount(workingEl, h("div", { class: "working", "data-testid": "working" }, h("span", { class: "dots" }, h("span"), h("span"), h("span")), label));
    if (!tick) {
      tick = setInterval(renderHeader, 1000);
    }
  }

  function renderTail() {
    const r = state.run;
    const refs = (r.artifact_refs || []).filter(Boolean);
    const artifactRefs = refs.filter((x) => x.startsWith("artifact://"));
    const needsActivity = !state.activityLoaded && !isRunLive(r.status) && !state.items.size;
    mount(
      tailEl,
      errorPanel(r),
      contractPanel(r),
      artifactRefs.length
        ? h(
            "div",
            { class: "refs" },
            artifactRefs.map((ref) => {
              const id = ref.slice("artifact://".length);
              return h("a", { class: "badge tone-accent", href: href(`/artifacts/${encodeURIComponent(id)}`) }, icon("package", { size: 12 }), id);
            }),
          )
        : null,
      needsActivity
        ? h("div", null, button(t("Show activity"), { size: "sm", variant: "ghost", iconName: "terminal", testid: "load-activity", onClick: () => onLoadActivity?.(state.run.id) }))
        : null,
      state.activityLoaded && !state.agentLive && !state.items.size && !isRunLive(r.status)
        ? h("p", { class: "subtle", style: "font-size:12.5px" }, t("No activity transcript was kept for this run; the result above comes from the durable run ledger."))
        : null,
    );
  }

  function renderFoot() {
    const r = state.run;
    const usage = r.usage || state.finished?.usage;
    mount(
      footEl,
      usage ? h("span", { title: JSON.stringify(usage) }, icon("zap", { size: 12 }), usageText(usage)) : null,
      r.finished_at ? h("span", { title: fmtDateTime(r.finished_at) }, icon("clock", { size: 12 }), `${t("finished")} ${fmtRelative(r.finished_at)}`) : null,
      r.account_id ? h("span", null, icon("users", { size: 12 }), r.account_id) : null,
      state.threadId ? h("span", { title: state.threadId }, icon("hash", { size: 12 }), `${t("thread")} ${shortSha(state.threadId)}`) : null,
    );
  }

  function renderRaw() {
    mount(
      rawEl,
      showRaw
        ? h(
            "div",
            { class: "stack", style: "gap:8px" },
            h("strong", { class: "subtle" }, t("Run record")),
            jsonView(state.run, { maxHeight: "260px" }),
            h("strong", { class: "subtle" }, t("Events ({n})", { n: state.events.length })),
            jsonView(state.events, { maxHeight: "320px" }),
          )
        : null,
    );
  }

  function renderAll() {
    el.dataset.status = state.run.status;
    renderHeader();
    renderResult();
    renderWorking();
    renderTail();
    renderFoot();
    renderRaw();
  }

  function newGroup() {
    const group = { items: new Set(), userToggled: false };
    group.summaryEl = h("span", { class: "worklog-summary" });
    group.listEl = h("div", { class: "worklog-items" });
    group.el = h(
      "details",
      {
        class: "worklog",
        "data-testid": "worklog",
        open: state.expanded || isRunLive(state.run.status) || null,
      },
      h("summary", null, icon("chevronRight", { size: 14, className: "chev" }), group.summaryEl),
      group.listEl,
    );
    group.el.addEventListener("click", (ev) => {
      if (ev.target.closest("summary")?.parentElement === group.el) group.userToggled = true;
    });
    groups.push(group);
    activityEl.append(group.el);
    return group;
  }

  function renderGroupSummary(group) {
    const items = [...group.items].map((key) => state.items.get(key)?.item).filter(Boolean);
    const count = (type) => items.filter((i) => i.type === type).length;
    const commands = count("command_execution");
    const files = items.filter((i) => i.type === "file_change").reduce((n, i) => n + (Array.isArray(i.changes) ? i.changes.length : 1), 0);
    const failed = items.filter(isFailedItem).length;
    const running = items.some((i) => i.status === "in_progress");
    const current = running ? [...items].reverse().find((i) => i.status === "in_progress") : null;
    mount(
      group.summaryEl,
      running ? icon("loader", { size: 14, className: "spin" }) : icon("terminal", { size: 14 }),
      h("span", { class: "worklog-title" }, running ? t("Working") : t("Worked")),
      h("span", { class: "worklog-meta" }, t("{n} steps", { n: items.length })),
      commands ? h("span", { class: "worklog-meta" }, t("{n} commands", { n: commands })) : null,
      files ? h("span", { class: "worklog-meta" }, t("{n} files", { n: files })) : null,
      failed ? badge(t("{n} failed", { n: failed }), { tone: "red" }) : null,
      current?.type === "command_execution" ? h("span", { class: "worklog-current mono" }, displayCommand(current.command)) : null,
    );
  }

  function upsertItem(item) {
    if (!item) return;
    const id = item.id || `anon-${state.items.size}`;
    const key = `${state.run.id}:${id}`;
    const prev = state.items.get(key);
    const merged = { ...(prev?.item || {}), ...item };
    const wasOpen = prev?.node?.open;
    const node = renderItem(merged, key, provider, wasOpen);
    let group = prev?.group ?? null;
    if (prev?.node) {
      prev.node.replaceWith(node);
    } else if (isWorkItem(merged)) {
      group = openGroup || (openGroup = newGroup());
      group.items.add(key);
      group.listEl.append(node);
    } else {
      openGroup = null;
      activityEl.append(node);
    }
    state.items.set(key, { item: merged, node, group });
    if (group) renderGroupSummary(group);
    onActivity?.();
  }

  function setExpanded(value) {
    state.expanded = value;
    for (const group of groups) if (!group.userToggled) group.el.open = value || isRunLive(state.run.status);
  }

  function notice(tone, title, body) {
    noticeEl.append(banner({ tone, title, body, testid: "run-notice" }));
  }

  renderPrompt();
  renderAll();

  return {
    el,
    runId: run.id,
    get run() {
      return state.run;
    },
    get hasActivity() {
      return state.items.size > 0;
    },
    update(next) {
      const wasLive = isRunLive(state.run.status);
      state.run = next;
      renderAll();
      if (wasLive && !isRunLive(next.status)) setExpanded(state.expanded);
    },
    setExpanded,
    setAgentLive(live) {
      if (state.agentLive === live) return;
      state.agentLive = live;
      renderTail();
    },
    setStreamState(value) {
      state.streamState = value;
      if (value === "live") state.activityLoaded = true;
      renderWorking();
      renderTail();
    },
    applyEvent(ev) {
      const data = ev.data || {};
      state.events.push(data);
      state.activityLoaded = true;
      switch (ev.type) {
        case "item.started":
        case "item.updated":
        case "item.completed":
          upsertItem(data.item);
          renderResult();
          break;
        case "thread.started":
          state.threadId = data.thread_id || null;
          renderFoot();
          break;
        case "turn.failed":
          notice("danger", t("The turn failed"), data.error?.message || "");
          break;
        case "error":
          notice("danger", t("Stream error"), data.message || "");
          break;
        case "sbx.error":
          notice("danger", t("Runner error"), data.message || "");
          break;
        case "sbx.unparsed":
          notice("warning", t("Unreadable event"), String(data.raw || "").slice(0, 300));
          break;
        case "sbx.turn_finished":
          state.finished = data;
          renderFoot();
          break;
        default:
          break;
      }
      if (showRaw) renderRaw();
      renderTail();
    },
    prompt(text) {
      prompts.set(agentId, state.run.id, text);
      renderPrompt();
    },
    destroy() {
      clearInterval(tick);
    },
  };
}

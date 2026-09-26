/** Reusable UI pieces built on `h()`. */
import { h, mount } from "./dom.js";
import { explainApiError, providerLabel, statusMeta } from "./domain.js";
import { icon } from "./icons.js";
import { t } from "./i18n.js";

/** Render `code` spans in UI copy as <code>; everything else stays text. */
export function rich(text) {
  if (typeof text !== "string" || !text.includes("`")) return text;
  return text
    .split(/(`[^`]+`)/g)
    .map((part) => (part.length > 2 && part.startsWith("`") && part.endsWith("`") ? h("code", null, part.slice(1, -1)) : part));
}

export function button(label, { variant = "secondary", size, iconName, onClick, type = "button", disabled, title, testid, busy } = {}) {
  return h(
    "button",
    {
      type,
      class: ["btn", `btn-${variant}`, size && `btn-${size}`, !label && "btn-icon", busy && "is-busy"],
      onClick,
      disabled: disabled || busy,
      title: title || (!label ? undefined : null),
      "aria-label": !label ? title : null,
      "data-testid": testid,
    },
    busy ? icon("loader", { className: "spin" }) : iconName ? icon(iconName) : null,
    label ? h("span", null, label) : null,
  );
}

export function linkButton(label, href, { variant = "secondary", size, iconName, testid, external } = {}) {
  return h(
    "a",
    {
      class: ["btn", `btn-${variant}`, size && `btn-${size}`],
      href,
      "data-testid": testid,
      target: external ? "_blank" : null,
      rel: external ? "noopener noreferrer" : null,
    },
    iconName ? icon(iconName) : null,
    h("span", null, label),
    external ? icon("external", { size: 13, className: "muted-icon" }) : null,
  );
}

/** Async click wrapper: shows a spinner and disables the button meanwhile. */
export function actionButton(label, run, opts = {}) {
  const btn = button(label, {
    ...opts,
    onClick: async () => {
      if (btn.disabled) return;
      const content = [...btn.childNodes];
      btn.disabled = true;
      btn.classList.add("is-busy");
      btn.replaceChildren(icon("loader", { className: "spin" }), ...(label ? [h("span", null, label)] : []));
      try {
        await run();
      } finally {
        if (btn.isConnected) {
          btn.disabled = Boolean(opts.disabled);
          btn.classList.remove("is-busy");
          btn.replaceChildren(...content);
        }
      }
    },
  });
  return btn;
}

export function statusBadge(kind, status, { testid } = {}) {
  const meta = statusMeta(kind, status);
  return h(
    "span",
    {
      class: ["status", `tone-${meta.tone}`, meta.live && "is-live"],
      title: status,
      "data-status": status,
      "data-testid": testid,
    },
    h("span", { class: "status-dot", "aria-hidden": "true" }),
    meta.label,
  );
}

export function badge(text, { tone = "neutral", title, mono, testid } = {}) {
  return h("span", { class: ["badge", `tone-${tone}`, mono && "mono"], title, "data-testid": testid }, text);
}

export function providerTag(provider) {
  return h(
    "span",
    { class: ["provider", `provider-${provider}`], title: provider },
    h("span", { class: "provider-dot", "aria-hidden": "true" }),
    providerLabel(provider),
  );
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = h("textarea", { value: text, style: "position:fixed;opacity:0" });
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast(t("Copied to clipboard"), { tone: "success", timeout: 1600 });
}

export function copyButton(text, { label, title } = {}) {
  return button(label || "", {
    variant: "ghost",
    size: "xs",
    iconName: "copy",
    title: title || t("Copy"),
    onClick: (ev) => {
      ev.stopPropagation();
      ev.preventDefault();
      void copyText(typeof text === "function" ? text() : text);
    },
  });
}

/** Monospace id/sha with a copy affordance. */
export function mono(value, { short, copy = true, testid } = {}) {
  if (!value) return h("span", { class: "muted" }, "—");
  const shown = short ? String(value).slice(0, short) : String(value);
  return h(
    "span",
    { class: "mono-chip", title: String(value), "data-testid": testid },
    h("code", null, shown),
    copy ? copyButton(String(value)) : null,
  );
}

// ---------------------------------------------------------------- toasts

let toastRoot = null;

export function toast(message, { tone = "neutral", detail, timeout = 4200 } = {}) {
  if (!toastRoot) {
    toastRoot = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(toastRoot);
  }
  const iconName = { success: "circleCheck", danger: "alert", warning: "warning" }[tone] || "info";
  const node = h(
    "div",
    { class: ["toast", `tone-${tone}`], "data-testid": "toast" },
    icon(iconName),
    h("div", { class: "toast-body" }, h("div", { class: "toast-title" }, message), detail ? h("div", { class: "toast-detail" }, detail) : null),
    button("", { variant: "ghost", size: "xs", iconName: "x", title: t("Dismiss"), onClick: () => node.remove() }),
  );
  toastRoot.append(node);
  if (timeout) setTimeout(() => node.remove(), timeout);
  return node;
}

export function toastError(err, prefix) {
  const { code, title, detail } = explainApiError(err);
  toast(prefix ? `${prefix}: ${title}` : title, {
    tone: "danger",
    detail: detail && detail !== title ? `${code} · ${detail}` : code,
    timeout: 8000,
  });
}

// --------------------------------------------------------------- dialogs

export function openDialog({ title, description, body, footer, size = "md", testid, onClose } = {}) {
  const dialog = h("dialog", { class: ["dialog", `dialog-${size}`], "data-testid": testid });
  const close = () => {
    if (dialog.open) dialog.close();
  };
  dialog.addEventListener("close", () => {
    dialog.remove();
    onClose?.();
  });
  dialog.addEventListener("click", (ev) => {
    if (ev.target === dialog) close();
  });
  mount(
    dialog,
    h(
      "div",
      { class: "dialog-panel" },
      h(
        "header",
        { class: "dialog-header" },
        h("div", null, h("h2", null, title), description ? h("p", { class: "muted" }, description) : null),
        button("", { variant: "ghost", size: "sm", iconName: "x", title: t("Close"), onClick: close }),
      ),
      h("div", { class: "dialog-body" }, body),
      footer ? h("footer", { class: "dialog-footer" }, footer) : null,
    ),
  );
  document.body.append(dialog);
  dialog.showModal();
  return { dialog, close };
}

export function confirmDialog({ title, body, confirmLabel = t("Confirm"), tone = "danger", testid = "confirm-dialog" }) {
  return new Promise((resolve) => {
    let result = false;
    const { close } = openDialog({
      title,
      size: "sm",
      testid,
      body: typeof body === "string" ? h("p", null, body) : body,
      footer: [
        button(t("Cancel"), { onClick: () => close() }),
        button(confirmLabel, {
          variant: tone === "danger" ? "danger" : "primary",
          testid: "confirm-ok",
          onClick: () => {
            result = true;
            close();
          },
        }),
      ],
      onClose: () => resolve(result),
    });
  });
}

// ------------------------------------------------------------- layout bits

export function pageHeader({ title, subtitle, actions, back, meta, testid }) {
  return h(
    "header",
    { class: "page-header" },
    back ? h("a", { class: "back-link", href: back.href }, icon("arrowLeft", { size: 14 }), back.label) : null,
    h(
      "div",
      { class: "page-header-row" },
      h("div", { class: "page-title" }, h("h1", { "data-testid": testid }, title), subtitle ? h("p", { class: "muted" }, rich(subtitle)) : null),
      actions ? h("div", { class: "page-actions" }, actions) : null,
    ),
    meta ? h("div", { class: "page-meta" }, meta) : null,
  );
}

export function card({ title, subtitle, actions, body, footer, class: cls, testid, iconName }) {
  return h(
    "section",
    { class: ["card", cls], "data-testid": testid },
    title
      ? h(
          "header",
          { class: "card-header" },
          h(
            "div",
            { class: "card-title" },
            iconName ? icon(iconName) : null,
            h("div", null, h("h2", null, title), subtitle ? h("p", { class: "muted" }, rich(subtitle)) : null),
          ),
          actions ? h("div", { class: "card-actions" }, actions) : null,
        )
      : null,
    body != null ? h("div", { class: "card-body" }, body) : null,
    footer ? h("footer", { class: "card-footer" }, footer) : null,
  );
}

export function kv(rows, { testid } = {}) {
  return h(
    "dl",
    { class: "kv", "data-testid": testid },
    rows
      .filter(Boolean)
      .map(([label, value]) => [h("dt", null, label), h("dd", null, value == null || value === "" ? h("span", { class: "muted" }, "—") : value)]),
  );
}

export function emptyState({ iconName = "info", title, body, actions, testid, compact }) {
  return h(
    "div",
    { class: ["empty", compact && "empty-compact"], "data-testid": testid },
    h("div", { class: "empty-icon" }, icon(iconName, { size: 22 })),
    h("h3", null, title),
    body ? h("p", { class: "muted" }, rich(body)) : null,
    actions ? h("div", { class: "empty-actions" }, actions) : null,
  );
}

export function banner({ tone = "info", title, body, actions, testid }) {
  const iconName = { danger: "alert", warning: "warning", success: "circleCheck" }[tone] || "info";
  return h(
    "div",
    { class: ["banner", `tone-${tone}`], role: tone === "danger" ? "alert" : "note", "data-testid": testid },
    icon(iconName),
    h("div", { class: "banner-body" }, title ? h("strong", null, rich(title)) : null, body ? h("div", null, rich(body)) : null),
    actions ? h("div", { class: "banner-actions" }, actions) : null,
  );
}

export function errorBanner(err, { retry, testid = "error-banner" } = {}) {
  const { code, title, detail } = explainApiError(err);
  return banner({
    tone: "danger",
    title,
    body: h("span", { class: "muted" }, detail && detail !== title ? `${code} · ${detail}` : code),
    actions: retry ? button(t("Retry"), { size: "sm", iconName: "refresh", onClick: retry }) : null,
    testid,
  });
}

export function skeleton(lines = 3) {
  return h(
    "div",
    { class: "skeleton", "aria-busy": "true", "aria-label": t("Loading") },
    Array.from({ length: lines }, (_, i) => h("div", { class: "skeleton-line", style: { width: `${90 - i * 12}%` } })),
  );
}

export function tabs(items, active, { onSelect, testid = "tabs" } = {}) {
  return h(
    "nav",
    { class: "tabs", role: "tablist", "data-testid": testid },
    items.map((item) =>
      h(
        "a",
        {
          class: ["tab", item.id === active && "is-active"],
          href: item.href,
          role: "tab",
          "aria-selected": String(item.id === active),
          "data-testid": `tab-${item.id}`,
          onClick: onSelect ? (ev) => onSelect(item.id, ev) : null,
        },
        item.iconName ? icon(item.iconName, { size: 15 }) : null,
        item.label,
        item.count != null ? h("span", { class: "tab-count" }, String(item.count)) : null,
      ),
    ),
  );
}

/**
 * Stamp every body cell with its column header as `data-label` so the
 * narrow-viewport card layout (styles.css) can keep values labelled once
 * the table stacks. Rows whose cell count differs from the header count
 * (e.g. colspan fillers) are left untouched.
 */
export function labelize(table) {
  const heads = [...table.querySelectorAll("thead th")].map((th) => th.textContent.trim());
  if (!heads.length) return table;
  table.querySelectorAll("tbody tr").forEach((tr) => {
    const cells = [...tr.children].filter((el) => el.tagName === "TD");
    if (cells.length !== heads.length || cells.some((td) => td.colSpan > 1)) return;
    cells.forEach((td, i) => td.setAttribute("data-label", heads[i]));
  });
  return table;
}

export function segmented(options, value, onChange, { testid, size } = {}) {
  const root = h("div", { class: ["segmented", size && `segmented-${size}`], role: "radiogroup", "data-testid": testid });
  const render = (current) =>
    mount(
      root,
      options.map((opt) =>
        h(
          "button",
          {
            type: "button",
            role: "radio",
            class: ["segment", opt.value === current && "is-active"],
            "aria-checked": String(opt.value === current),
            disabled: opt.disabled,
            title: opt.title,
            "data-value": opt.value,
            onClick: () => {
              render(opt.value);
              onChange(opt.value);
            },
          },
          opt.iconName ? icon(opt.iconName, { size: 14 }) : null,
          opt.label,
        ),
      ),
    );
  render(value);
  root.setValue = render;
  return root;
}

export function field(label, control, { hint, error, required, htmlFor, testid, inline } = {}) {
  return h(
    "div",
    { class: ["field", inline && "field-inline"], "data-testid": testid },
    label ? h("label", { class: "field-label", for: htmlFor }, label, required ? h("span", { class: "req", "aria-hidden": "true" }, " *") : null) : null,
    control,
    error ? h("p", { class: "field-error" }, error) : hint ? h("p", { class: "field-hint" }, rich(hint)) : null,
  );
}

export function toggle(label, checked, onChange, { hint, disabled, testid } = {}) {
  const input = h("input", {
    type: "checkbox",
    role: "switch",
    checked,
    disabled,
    "data-testid": testid,
    onChange: (ev) => onChange(ev.target.checked),
  });
  return h(
    "label",
    { class: ["switch", disabled && "is-disabled"] },
    input,
    h("span", { class: "switch-track", "aria-hidden": "true" }, h("span", { class: "switch-thumb" })),
    h("span", { class: "switch-text" }, h("span", null, label), hint ? h("span", { class: "field-hint" }, hint) : null),
  );
}

export function progressBar(value, max, { tone = "accent", label } = {}) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return h(
    "div",
    { class: "meter", title: label || `${value} / ${max}` },
    h("div", { class: ["meter-fill", `tone-${tone}`], style: { width: `${pct}%` } }),
  );
}

// ------------------------------------------------------------ JSON viewer

function jsonNode(value, indent) {
  const pad = "  ".repeat(indent);
  if (value === null) return [h("span", { class: "j-null" }, "null")];
  if (typeof value === "string") return [h("span", { class: "j-str" }, JSON.stringify(value))];
  if (typeof value === "number") return [h("span", { class: "j-num" }, String(value))];
  if (typeof value === "boolean") return [h("span", { class: "j-bool" }, String(value))];
  if (Array.isArray(value)) {
    if (!value.length) return ["[]"];
    const out = ["[\n"];
    value.forEach((v, i) => {
      out.push(`${pad}  `, ...jsonNode(v, indent + 1), i < value.length - 1 ? ",\n" : "\n");
    });
    out.push(`${pad}]`);
    return out;
  }
  const entries = Object.entries(value);
  if (!entries.length) return ["{}"];
  const out = ["{\n"];
  entries.forEach(([k, v], i) => {
    out.push(`${pad}  `, h("span", { class: "j-key" }, JSON.stringify(k)), ": ", ...jsonNode(v, indent + 1), i < entries.length - 1 ? ",\n" : "\n");
  });
  out.push(`${pad}}`);
  return out;
}

export function jsonView(value, { testid, maxHeight } = {}) {
  return h(
    "div",
    { class: "code-block", "data-testid": testid },
    h("div", { class: "code-block-actions" }, copyButton(() => JSON.stringify(value, null, 2))),
    h("pre", { class: "json", style: maxHeight ? { maxHeight } : null }, h("code", null, jsonNode(value, 0))),
  );
}

export function codeBlock(text, { testid, lang } = {}) {
  return h(
    "div",
    { class: "code-block", "data-testid": testid, "data-lang": lang },
    h("div", { class: "code-block-actions" }, copyButton(text)),
    h("pre", null, h("code", null, text)),
  );
}

/** Visibility-aware polling: skips ticks while the tab is hidden. */
export function poller(fn, intervalMs) {
  let timer = null;
  let stopped = false;
  let running = false;
  const tick = async () => {
    if (stopped) return;
    if (document.visibilityState === "visible" && !running) {
      running = true;
      try {
        await fn();
      } catch {
        // Surface errors inside fn; polling keeps going.
      } finally {
        running = false;
      }
    }
    if (!stopped) timer = setTimeout(tick, typeof intervalMs === "function" ? intervalMs() : intervalMs);
  };
  timer = setTimeout(tick, typeof intervalMs === "function" ? intervalMs() : intervalMs);
  return {
    stop() {
      stopped = true;
      clearTimeout(timer);
    },
    now() {
      clearTimeout(timer);
      void tick();
    },
  };
}

/** Shared create-form pieces used by the task and (raw) agent forms. */
import { h, mount } from "../lib/dom.js";
import { CANONICAL_EFFORTS } from "../lib/domain.js";
import { t } from "../lib/i18n.js";
import { icon } from "../lib/icons.js";

export const SHA = /^[0-9a-f]{40}$/;
export const DEFAULT_SCHEMA = `{
  "type": "object",
  "properties": {
    "summary": { "type": "string" },
    "files_changed": { "type": "array", "items": { "type": "string" } }
  },
  "required": ["summary"]
}`;

export function uuid() {
  return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function num(value) {
  if (value === "" || value == null) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : NaN;
}

export function rangeValue(min, max) {
  const a = num(min);
  const b = num(max);
  if (a == null && b == null) return undefined;
  if (a != null && b != null) return a === b ? a : [a, b];
  return a ?? b;
}

/** Comma/Enter separated token input. */
export function chipInput(values, onChange, { placeholder, testid }) {
  const root = h("div", { class: "chip-input", "data-testid": testid });
  const input = h("input", {
    placeholder,
    onKeydown: (ev) => {
      if ((ev.key === "Enter" || ev.key === ",") && input.value.trim()) {
        ev.preventDefault();
        add(input.value);
      } else if (ev.key === "Backspace" && !input.value && values.length) {
        values.pop();
        onChange(values);
        render();
      }
    },
    onBlur: () => input.value.trim() && add(input.value),
  });
  function add(raw) {
    for (const part of raw.split(",")) {
      const v = part.trim();
      if (v && !values.includes(v)) values.push(v);
    }
    input.value = "";
    onChange(values);
    render();
    input.focus();
  }
  function render() {
    mount(
      root,
      values.map((v) =>
        h(
          "span",
          { class: "chip" },
          v,
          h("button", { type: "button", "aria-label": t("Remove"), onClick: () => {
            values.splice(values.indexOf(v), 1);
            onChange(values);
            render();
          } }, icon("x", { size: 12 })),
        ),
      ),
      input,
    );
  }
  render();
  root.addEventListener("click", () => input.focus());
  return root;
}

// /v1/models rows are per (account, model) — dedupe across accounts when the
// scheduler picks one (auto), else pin to the selected account so only its
// servable models are offered.
export function providerModels(models, provider, accountId) {
  const rows = models.filter((m) => m.provider === provider);
  if (!accountId || accountId === "auto") {
    const byModel = new Map();
    for (const r of rows) {
      const cur = byModel.get(r.model);
      if (!cur || (r.accounts_available || 0) > (cur.accounts_available || 0)) {
        byModel.set(r.model, r);
      }
    }
    return [...byModel.values()];
  }
  return rows.filter((m) => !m.account || m.account === accountId);
}

// Canonical ladder filtered to what the selected account/model rows actually
// advertise — unsupported levels are hidden, never offered.
export function effortOptions(rows) {
  const supported = new Set();
  for (const r of rows) for (const e of r.reasoning_efforts || []) supported.add(e);
  return CANONICAL_EFFORTS.filter((e) => supported.has(e));
}

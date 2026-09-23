/** Tiny DOM builder — no framework, no build step. Never assigns innerHTML. */

const BOOL_PROPS = new Set([
  "disabled",
  "checked",
  "selected",
  "hidden",
  "required",
  "readOnly",
  "open",
  "multiple",
  "autofocus",
  "indeterminate",
]);

function classNames(value) {
  if (!value) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(classNames).filter(Boolean).join(" ");
  return Object.entries(value)
    .filter(([, on]) => on)
    .map(([name]) => name)
    .join(" ");
}

export function append(parent, children) {
  for (const child of children) {
    if (child == null || child === false || child === true) continue;
    if (Array.isArray(child)) append(parent, child);
    else if (child instanceof Node) parent.append(child);
    else parent.append(document.createTextNode(String(child)));
  }
  return parent;
}

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value == null || value === false) continue;
      if (key === "class") {
        const cls = classNames(value);
        if (cls) el.className = cls;
      } else if (key === "style") {
        if (typeof value === "string") el.setAttribute("style", value);
        else Object.assign(el.style, value);
      } else if (key === "dataset") {
        for (const [dk, dv] of Object.entries(value)) {
          if (dv != null) el.dataset[dk] = String(dv);
        }
      } else if (key === "ref") {
        value(el);
      } else if (key.startsWith("on") && typeof value === "function") {
        el.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === "value") {
        el.value = String(value);
      } else if (BOOL_PROPS.has(key)) {
        el[key] = Boolean(value);
      } else if (key === "text") {
        el.textContent = String(value);
      } else {
        el.setAttribute(key, value === true ? "" : String(value));
      }
    }
  }
  return append(el, children);
}

export function mount(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

export function frag(...children) {
  return append(document.createDocumentFragment(), children);
}

/**
 * `value` normalized if it is an absolute http(s) URL, else null. Use it for
 * every href / window.open built from API data: some of it (e.g. a recorded
 * pull request's URL) is produced inside agent sandboxes, and a `javascript:`
 * link would run with access to the stored API key.
 */
export function httpUrl(value) {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

/** Debounce helper for inputs that trigger work. */
export function debounce(fn, ms = 200) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

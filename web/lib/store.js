/** Small persisted caches that make the console nicer to use. */

const PROMPTS_KEY = "sbx.console.prompts";
const WORKFLOWS_KEY = "sbx.console.workflows";
const MAX_PROMPTS = 300;

function readJson(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

/**
 * Prompts sent from this browser, cached locally (per agent/run) so the
 * conversation renders instantly before the run's ``prompt`` field loads.
 */
export const prompts = {
  get(agentId, runId) {
    return readJson(PROMPTS_KEY, {})[`${agentId}:${runId}`] ?? null;
  },
  set(agentId, runId, text) {
    const all = readJson(PROMPTS_KEY, {});
    all[`${agentId}:${runId}`] = text;
    const keys = Object.keys(all);
    for (const key of keys.slice(0, Math.max(0, keys.length - MAX_PROMPTS))) delete all[key];
    localStorage.setItem(PROMPTS_KEY, JSON.stringify(all));
  },
  clear() {
    localStorage.removeItem(PROMPTS_KEY);
  },
};

export const recentWorkflows = {
  list() {
    return readJson(WORKFLOWS_KEY, []);
  },
  add(id) {
    const next = [id, ...recentWorkflows.list().filter((x) => x !== id)].slice(0, 12);
    localStorage.setItem(WORKFLOWS_KEY, JSON.stringify(next));
  },
  clear() {
    localStorage.removeItem(WORKFLOWS_KEY);
  },
};

const THEME_KEY = "sbx.console.theme";

export function getTheme() {
  const v = localStorage.getItem(THEME_KEY);
  return v === "light" || v === "dark" ? v : "system";
}

export function applyTheme(theme = getTheme()) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.dataset.theme = theme;
}

export function setTheme(theme) {
  if (theme === "system") localStorage.removeItem(THEME_KEY);
  else localStorage.setItem(THEME_KEY, theme);
  applyTheme(theme);
}

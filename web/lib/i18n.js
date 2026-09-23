/**
 * English source strings are the message ids; `ZH` maps them to Simplified
 * Chinese. `{name}` placeholders interpolate from `vars`.
 */
import { ZH } from "./i18n-zh.js";

const LANG_KEY = "sbx.console.lang";
const listeners = new Set();

function detect() {
  const stored = localStorage.getItem(LANG_KEY);
  if (stored === "en" || stored === "zh-CN") return stored;
  const nav = (navigator.languages && navigator.languages[0]) || navigator.language || "en";
  return nav.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
}

let lang = detect();
document.documentElement.lang = lang;

export function getLang() {
  return lang;
}

export function setLang(next) {
  if (next !== "en" && next !== "zh-CN") return;
  lang = next;
  localStorage.setItem(LANG_KEY, next);
  document.documentElement.lang = next;
  for (const fn of listeners) fn(next);
}

export function onLangChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function t(text, vars) {
  let out = lang === "zh-CN" && ZH[text] ? ZH[text] : text;
  if (vars) {
    out = out.replace(/\{(\w+)\}/g, (m, name) => (name in vars ? String(vars[name]) : m));
  }
  return out;
}

/** Plural-aware helper: `tn(n, "{n} run", "{n} runs")`. */
export function tn(n, one, many) {
  return t(n === 1 ? one : many, { n });
}

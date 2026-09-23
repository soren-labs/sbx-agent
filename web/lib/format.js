import { getLang } from "./i18n.js";

function locale() {
  return getLang() === "zh-CN" ? "zh-CN" : "en";
}

export function toDate(value) {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function fmtRelative(value, now = Date.now()) {
  const d = toDate(value);
  if (!d) return "—";
  const diff = Math.round((d.getTime() - now) / 1000);
  const abs = Math.abs(diff);
  const rtf = new Intl.RelativeTimeFormat(locale(), { numeric: "auto" });
  if (abs < 45) return rtf.format(0, "second");
  if (abs < 45 * 60) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 22 * 3600) return rtf.format(Math.round(diff / 3600), "hour");
  if (abs < 7 * 86400) return rtf.format(Math.round(diff / 86400), "day");
  return fmtDate(d);
}

export function fmtDate(value) {
  const d = toDate(value);
  if (!d) return "—";
  return d.toLocaleDateString(locale(), { month: "short", day: "numeric", year: "numeric" });
}

export function fmtDateTime(value) {
  const d = toDate(value);
  if (!d) return "—";
  return d.toLocaleString(locale(), {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function fmtDuration(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1) return `${Math.round(n * 1000)} ms`;
  if (n < 60) return `${n < 10 ? n.toFixed(1) : Math.round(n)} s`;
  const s = Math.round(n);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h) return `${h} h ${m} m`;
  return r ? `${m} m ${r} s` : `${m} m`;
}

export function secondsBetween(start, end) {
  const a = toDate(start);
  const b = end ? toDate(end) : new Date();
  if (!a || !b) return null;
  return Math.max(0, (b.getTime() - a.getTime()) / 1000);
}

export function fmtUsd(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n === 0) return "$0.00";
  if (Math.abs(n) < 0.0001) return "<$0.0001";
  if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

export function fmtNumber(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString(locale());
}

export function fmtCompact(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return new Intl.NumberFormat(locale(), { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

export function fmtBytes(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function shortSha(sha) {
  return typeof sha === "string" && sha ? sha.slice(0, 8) : "—";
}

export function fmtMemory(mib) {
  const n = Number(mib);
  if (!Number.isFinite(n)) return "—";
  return n >= 1024 ? `${(n / 1024).toFixed(n % 1024 ? 1 : 0)} GiB` : `${n} MiB`;
}

/** `[min, max]` → "1–2" (or the scalar when equal). */
export function fmtRange(pair, unit = (v) => String(v)) {
  if (!Array.isArray(pair)) return pair == null ? "—" : unit(pair);
  const [a, b] = pair;
  if (b == null || a === b) return unit(a);
  return `${unit(a)} – ${unit(b)}`;
}

export function totalTokens(usage) {
  if (!usage) return null;
  return (usage.input_tokens || 0) + (usage.output_tokens || 0);
}

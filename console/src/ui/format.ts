/** Compact relative timestamps: "just now", "2m ago", "yesterday", "Oct 12". */
export function formatRelative(iso: string | null | undefined, locale = "en"): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const zh = locale.startsWith("zh");
  const diff = Date.now() - t;
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return zh ? "刚刚" : "just now";
  if (mins < 60) return zh ? `${mins}分钟前` : `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return zh ? `${hours}小时前` : `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 1.5) return zh ? "昨天" : "yesterday";
  if (days < 7) return zh ? `${days}天前` : `${days}d ago`;
  return new Intl.DateTimeFormat(zh ? "zh-CN" : "en", { month: "short", day: "numeric" }).format(t);
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(-8) : "—";
}

export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

/** Absolute timestamp for tooltips and detail rows. */
export function when(iso: string | null | undefined, locale = "en"): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat(locale.startsWith("zh") ? "zh-CN" : "en", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(d);
}

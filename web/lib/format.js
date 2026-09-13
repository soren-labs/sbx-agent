export const STATUS_LABELS = {
  creating: "创建中",
  idle: "空闲",
  running: "进行中",
  closed: "已关闭",
  timed_out: "已超时",
  lost: "已丢失",
};

export const ERROR_MESSAGES = {
  401: "未授权（401）：请检查控制面登录凭据。",
  404: "会话不存在（404）。",
  409: "上一轮还在进行中（409）。请等待完成，或先点「停止」。",
  429: "并发会话已达上限 2（429）。请先关闭一个进行中的会话。",
};

export function statusLabel(status) {
  return STATUS_LABELS[status] || status || "未知";
}

export function isTerminal(status) {
  return status === "closed" || status === "timed_out" || status === "lost";
}

export function formatUsd(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return "$0.00";
  if (Math.abs(n) < 0.01) return `$${n.toFixed(6)}`;
  return `$${n.toFixed(4)}`;
}

export function formatDuration(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${r}s`;
  return `${r}s`;
}

export function formatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatUsage(usage) {
  if (!usage) return "—";
  const input = usage.input_tokens ?? 0;
  const cached = usage.cached_input_tokens ?? 0;
  const output = usage.output_tokens ?? 0;
  return `in ${input} · cache ${cached} · out ${output}`;
}

export function friendlyHttpError(status, body) {
  if (ERROR_MESSAGES[status]) return ERROR_MESSAGES[status];
  const code = body && typeof body.code === "number" ? body.code : status;
  const err = body && body.error ? String(body.error) : "请求失败";
  return `${err}（${code}）`;
}

export const MODELS = ["gpt-5.6-luna", "gpt-5"];

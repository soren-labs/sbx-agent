import React from "react";
import type { AgentStatus, RunStatus, AccountStatus, ErrorBody } from "./mock";

export function fmtAgo(isoStr?: string | null): string {
  if (!isoStr) return "—";
  const s = Math.max(0, (Date.now() - new Date(isoStr).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)}s ago`;
  const m = s / 60;
  if (m < 90) return `${Math.round(m)}m ago`;
  const h = m / 60;
  if (h < 48) return `${Math.round(h)}h ago`;
  return `${Math.round(h / 24)}d ago`;
}
export const fmtTs = (s?: string | null) => (s ? new Date(s).toLocaleString() : "—");
export const fmtTok = (n?: number | null) => (n == null ? "—" : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`);
export const fmtUsd = (n?: number | null) => (n == null ? "—" : `$${n.toFixed(2)}`);
export const shortSha = (s?: string | null) => (s ? s.slice(0, 7) : "—");
export const trunc = (s: string, n = 22) => (s.length > n ? s.slice(0, n) + "…" : s);

const AGENT_TONE: Record<AgentStatus, string> = {
  creating: "blue", idle: "cyan", running: "green", closed: "gray", timed_out: "amber", lost: "red",
};
const RUN_TONE: Record<RunStatus, string> = {
  CREATING: "blue", RUNNING: "green", FINISHED: "cyan", ERROR: "red", CANCELLED: "amber", EXPIRED: "gray", UNKNOWN: "violet",
};
const ACCT_TONE: Record<AccountStatus, string> = {
  active: "green", cooling: "amber", invalid: "red", disabled: "gray",
};
export const Badge = ({ tone = "gray", children }: { tone?: string; children: React.ReactNode }) => (
  <span className={`badge ${tone}`}>{children}</span>
);
export const AgentBadge = ({ s }: { s: AgentStatus }) => <Badge tone={AGENT_TONE[s]}>{s}</Badge>;
export const RunBadge = ({ s }: { s: RunStatus }) => <Badge tone={RUN_TONE[s]}>{s}</Badge>;
export const AcctBadge = ({ s }: { s: AccountStatus }) => <Badge tone={ACCT_TONE[s]}>{s}</Badge>;
export const ProviderBadge = ({ p }: { p: string }) => <Badge tone="violet">{p}</Badge>;

export const Card = ({ title, children, actions }: { title?: React.ReactNode; children: React.ReactNode; actions?: React.ReactNode }) => (
  <div className="card">
    {(title || actions) && (
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
        {title && <h3 style={{ margin: 0 }}>{title}</h3>}
        {actions}
      </div>
    )}
    {children}
  </div>
);

export const KV = ({ rows }: { rows: [string, React.ReactNode][] }) => (
  <dl className="kv">
    {rows.map(([k, v]) => (
      <React.Fragment key={k}>
        <dt>{k}</dt>
        <dd>{v}</dd>
      </React.Fragment>
    ))}
  </dl>
);

export const Empty = ({ title, hint, action }: { title: string; hint: string; action?: React.ReactNode }) => (
  <div className="empty">
    <div className="big">{title}</div>
    <div>{hint}</div>
    {action && <div className="mt">{action}</div>}
  </div>
);

export const ErrorCard = ({ body, http }: { body: ErrorBody; http?: number }) => (
  <div className="alert red">
    <div className="row" style={{ justifyContent: "space-between" }}>
      <strong>
        {http ? `HTTP ${http} · ` : ""}
        <code>{body.error.code}</code>
      </strong>
      {body.error.retry_after != null && <span className="small">retry_after: {body.error.retry_after}s</span>}
    </div>
    <div className="mt" style={{ marginTop: 6 }}>{body.error.message}</div>
  </div>
);

export const JsonView = ({ value }: { value: unknown }) => (
  <pre className="json-view">{JSON.stringify(value, null, 2)}</pre>
);

export const Tabs = ({ tabs, cur, onSel }: { tabs: string[]; cur: string; onSel: (t: string) => void }) => (
  <div className="tabs">
    {tabs.map((t) => (
      <div key={t} className={`tab ${t === cur ? "active" : ""}`} onClick={() => onSel(t)}>
        {t}
      </div>
    ))}
  </div>
);

export const Field = ({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) => (
  <div className="field">
    <label>{label}</label>
    {children}
    {hint && <div className="hint">{hint}</div>}
  </div>
);

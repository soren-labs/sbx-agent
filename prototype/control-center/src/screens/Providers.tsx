import React, { useContext } from "react";
import { AppCtx } from "../App";
import { ACCOUNTS, MODELS, PROVIDERS } from "../mock";
import { AcctBadge, Badge, Card, Empty, fmtAgo, fmtTs } from "../ui";

export default function Providers() {
  const { scenario } = useContext(AppCtx);
  const accounts = scenario === "coldstart" ? [] : ACCOUNTS;

  return (
    <div>
      <h1>Providers &amp; accounts</h1>
      <div className="sub">
        Support matrix (<code>docs/providers.md</code>), account pool (<code>/v1/accounts</code>) and model
        availability (<code>/v1/models</code>). Credential values are never shown — only status.
      </div>

      <Card title="Provider support matrix — v0.1.1">
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead>
              <tr><th>Provider</th><th>Status</th><th>CLI / pin</th><th>Auth file</th><th>Multi-turn</th><th>Evidence</th></tr>
            </thead>
            <tbody>
              {PROVIDERS.map((p) => {
                const accts = accounts.filter((a) => a.provider === p.id);
                const ready = accts.some((a) => a.status === "active" && a.running < a.max_concurrent);
                return (
                  <tr key={p.id}>
                    <td className="mono">{p.id}</td>
                    <td><Badge tone={p.status === "Stable" ? "green" : "amber"}>{p.status}</Badge></td>
                    <td className="small">{p.cli}</td>
                    <td className="mono small">{p.authFile}</td>
                    <td className="small mono">{p.multiTurn}</td>
                    <td className="small muted" style={{ maxWidth: 300 }}>{p.evidence}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <div className="small muted mt">
          Evidence policy: Stable requires a passing real-account Modal E2E on the release tag — fakes and replays never count.
        </div>
      </Card>

      <div className="grid cols-2 section mt">
        <Card
          title="Accounts (admin scope)"
          actions={<button className="btn sm">+ Import account</button>}
        >
          {accounts.length === 0 ? (
            <Empty
              title="No accounts imported"
              hint="POST /v1/accounts {provider, label, credential:{files:{…}}} — the credential blob is never echoed back."
            />
          ) : (
            <table>
              <thead>
                <tr><th>ID</th><th>Provider</th><th>Label</th><th>Status</th><th>Slots</th><th>Last used</th><th></th></tr>
              </thead>
              <tbody>
                {accounts.map((a) => (
                  <tr key={a.id}>
                    <td className="mono small">{a.id}</td>
                    <td className="mono small">{a.provider}</td>
                    <td className="small">{a.label}</td>
                    <td>
                      <AcctBadge s={a.status} />
                      {a.last_error && <div className="small muted mono">{a.last_error}</div>}
                      {a.cooldown_until && <div className="small muted">until {fmtTs(a.cooldown_until)}</div>}
                    </td>
                    <td className="small">{a.running}/{a.max_concurrent}</td>
                    <td className="small muted">{fmtAgo(a.last_used_at)}</td>
                    <td>
                      <div className="row">
                        <button className="btn sm ghost" title="POST /v1/accounts/{id}/verify — probe in a throwaway sandbox">verify</button>
                        <button className="btn sm ghost danger">remove</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card title="Models — GET /v1/models">
          <table>
            <thead><tr><th>Provider</th><th>Model</th><th>Accounts available</th></tr></thead>
            <tbody>
              {MODELS.map((m) => (
                <tr key={`${m.provider}/${m.model}`}>
                  <td className="mono small">{m.provider}</td>
                  <td className="mono small">{m.model}</td>
                  <td>
                    {m.accounts_available === 0 ? (
                      <Badge tone="red">0 — not schedulable</Badge>
                    ) : (
                      <Badge tone="green">{m.accounts_available}</Badge>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="small muted mt">
            accounts_available counts <code>active</code> accounts with a free slot for that model —
            0 means a create would 429 <code>provider_exhausted</code>.
          </div>
        </Card>
      </div>

      <Card title="Credential lifecycle">
        <div className="grid cols-4">
          {[
            ["import", "Blob travels over HTTPS into your Modal workspace; mounted as Secret sbx-acct-<id>; restored at 0600 in the sandbox."],
            ["verify", "POST /v1/accounts/{id}/verify probes in a throwaway sandbox — static / sandbox / auth (authoritative provider check)."],
            ["cooldown", "auth_invalid / rate_limited put the account in cooldown; the scheduler skips it until cooldown_until."],
            ["write-back", "runner export-credentials writes refreshed OAuth material back to the account Secret (SOR-147)."],
          ].map(([t, d]) => (
            <div key={t}>
              <div className="mono small" style={{ color: "var(--accent)", marginBottom: 6 }}>{t}</div>
              <div className="small muted">{d}</div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

import React, { useContext, useState } from "react";
import { AppCtx } from "../App";
import { API_KEYS, ME } from "../mock";
import { Badge, Card, Empty, Field, fmtTs } from "../ui";

export default function ApiKeys() {
  const { scenario } = useContext(AppCtx);
  const keys = scenario === "coldstart" ? [API_KEYS[0]] : API_KEYS;
  const [showCreate, setShowCreate] = useState(false);
  const [createdOnce, setCreatedOnce] = useState(false);
  const [label, setLabel] = useState("");
  const [scopeAdmin, setScopeAdmin] = useState(false);

  return (
    <div>
      <h1>API keys &amp; admin</h1>
      <div className="sub">
        <code>Authorization: Bearer sbx_&lt;key&gt;</code> — the control plane stores{" "}
        <code>sha256(key)</code> only. <code>admin</code> scope gates accounts / api-keys / verify.
      </div>

      <div className="grid cols-3 section">
        <Card title="This key — GET /v1/me">
          <div className="mono">{ME.key_id}</div>
          <div className="small muted">{ME.label}</div>
          <div className="row" style={{ marginTop: 8 }}>
            {ME.scopes.map((s) => <Badge key={s} tone={s === "admin" ? "violet" : "blue"}>{s}</Badge>)}
          </div>
        </Card>
        <Card title="Deployment">
          <div className="small">
            Edge: <code>https://sbx.sorenforge.com</code>
            <div className="muted mt">Cloudflare Worker → sbx-control (FastAPI, scales to 0)</div>
            <div className="muted">Version: v0.1.1 (public alpha)</div>
          </div>
        </Card>
        <Card title="Auth model">
          <div className="small muted">
            Per-key Bearer auth; plaintext returned exactly once at create. <code>/api/*</code> is the
            legacy internal surface for the bundled dashboard only — new integrations go on{" "}
            <code>/v1</code>.
          </div>
        </Card>
      </div>

      <Card
        title="Keys (admin scope) — GET /v1/api-keys"
        actions={<button className="btn sm primary" onClick={() => setShowCreate(!showCreate)}>+ Create key</button>}
      >
        {showCreate && (
          <div className="card" style={{ background: "#0e121c", marginBottom: 14 }}>
            <div className="grid cols-3">
              <Field label="Label"><input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="ci-orchestrator" /></Field>
              <Field label="Scopes">
                <div className="row small" style={{ paddingTop: 6 }}>
                  <label><input type="checkbox" checked readOnly /> agents (default)</label>
                  <label><input type="checkbox" checked={scopeAdmin} onChange={(e) => setScopeAdmin(e.target.checked)} /> admin</label>
                </div>
              </Field>
              <Field label=" ">
                <button className="btn primary sm" onClick={() => setCreatedOnce(true)}>Create</button>
              </Field>
            </div>
            {createdOnce && (
              <div className="alert amber">
                <strong>Shown once — copy now:</strong>{" "}
                <code>sbx_m0ck_7f3k2…REDACTED</code> — only <code>sha256(key)</code> is persisted;
                it cannot be recovered later.
              </div>
            )}
          </div>
        )}
        {keys.length === 0 ? (
          <Empty title="No keys" hint="sbx deploy mints a bootstrap admin key into <state>/bootstrap.key (mode 0600)." />
        ) : (
          <table>
            <thead><tr><th>ID</th><th>Label</th><th>Scopes</th><th>Created</th><th>Revoked</th><th></th></tr></thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id} style={{ opacity: k.revoked_at ? 0.5 : 1 }}>
                  <td className="mono small">{k.id}</td>
                  <td>{k.label}</td>
                  <td>{k.scopes.map((s) => <Badge key={s} tone={s === "admin" ? "violet" : "blue"}>{s}</Badge>)}</td>
                  <td className="small muted">{fmtTs(k.created_at)}</td>
                  <td className="small muted">{k.revoked_at ? fmtTs(k.revoked_at) : "—"}</td>
                  <td>{!k.revoked_at && <button className="btn sm ghost danger">revoke</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="small muted mt">
          Hash listing only — no plaintext in any list response. Revoke:{" "}
          <code>DELETE /v1/api-keys/{"{id}"}</code> → 204.
        </div>
      </Card>

      <Card title="Guards">
        <div className="grid cols-3">
          <div className="small muted"><Badge tone="red">401 unauthorized</Badge><div className="mt">Missing/wrong key, or revoked. Verify with GET /v1/me.</div></div>
          <div className="small muted"><Badge tone="amber">403 forbidden</Badge><div className="mt">Valid key missing admin scope — accounts, api-keys and verify are admin-only.</div></div>
          <div className="small muted"><Badge tone="blue">scopes</Badge><div className="mt"><code>agents</code>: create/list/operate agents, runs, workspaces, artifacts, workflows. <code>admin</code>: accounts + keys.</div></div>
        </div>
      </Card>
    </div>
  );
}

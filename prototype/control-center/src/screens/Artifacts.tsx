import React, { useContext, useState } from "react";
import { AppCtx } from "../App";
import { ARTIFACTS, RUNS } from "../mock";
import { Badge, Card, Empty, fmtTs, JsonView, KV, shortSha, Tabs } from "../ui";

export default function Artifacts({ id }: { id?: string }) {
  const { scenario, nav } = useContext(AppCtx);
  const arts = scenario === "coldstart" ? [] : ARTIFACTS;
  const art = arts.find((a) => a.artifact_id === id) ?? arts[0];
  const [tab, setTab] = useState("Manifest");

  // Structured output evidence view: pull the producing run's output_contract + structured_output
  const producingRun = art?.producer.run_id
    ? Object.values(RUNS).flat().find((r) => r.id === art.producer.run_id)
    : undefined;

  if (!art) {
    return (
      <div>
        <h1>Artifacts &amp; evidence</h1>
        <div className="sub"><code>GET /v1/artifacts</code> — durable packages that outlive sandboxes</div>
        <Empty
          title="No artifacts"
          hint="POST /v1/agents/{id}/artifacts collects the workspace while the sandbox is alive — manifest, patch.diff, repo.bundle, files/."
        />
      </div>
    );
  }

  return (
    <div>
      <h1>Artifacts &amp; evidence</h1>
      <div className="sub">
        Durable manifests (<code>/v1/artifacts</code>) plus run-level structured output and contract
        verdicts (SOR-130).
      </div>

      <div className="grid" style={{ gridTemplateColumns: "320px 1fr" }}>
        <div className="grid">
          {arts.map((a) => (
            <div
              key={a.artifact_id}
              className="card"
              style={{ cursor: "pointer", borderColor: a.artifact_id === art.artifact_id ? "var(--accent)" : undefined }}
              onClick={() => nav(`/artifacts/${a.artifact_id}`)}
            >
              <div className="row" style={{ justifyContent: "space-between" }}>
                <code>{a.artifact_id}</code>
                <Badge tone="cyan">{a.format}</Badge>
              </div>
              <div className="small muted" style={{ marginTop: 6 }}>
                {shortSha(a.base_sha)} → {shortSha(a.head_sha)} · {a.files.length} files · {fmtTs(a.created_at)}
              </div>
              <div className="small muted">by {a.producer.agent_id}{a.producer.run_id ? ` / ${a.producer.run_id}` : ""}</div>
            </div>
          ))}
        </div>

        <div>
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
            <h2 className="mono" style={{ margin: 0 }}>{art.artifact_id}</h2>
            <div className="row">
              <button className="btn sm">Download patch.diff</button>
              <button className="btn sm">manifest.json</button>
              {art.payloads["repo.bundle"] && <button className="btn sm">repo.bundle</button>}
            </div>
          </div>

          <Tabs tabs={["Manifest", "Files", "Tests & warnings", "Structured output"]} cur={tab} onSel={setTab} />

          {tab === "Manifest" && (
            <Card title="manifest.json — checksums verified on read">
              <KV rows={[
                ["schema_version", String(art.schema_version)],
                ["repo", <code>{art.repo}</code>],
                ["base_sha", <code>{shortSha(art.base_sha)}</code>],
                ["head_sha", <code>{shortSha(art.head_sha)}</code>],
                ["producer", <code>{art.producer.agent_id} / {art.producer.run_id}</code>],
                ["created_at", fmtTs(art.created_at)],
                ["download_url", <code>{art.download_url}?member=…</code>],
              ]} />
              <h3 className="mt">Payloads (member → sha256)</h3>
              <JsonView value={art.payloads} />
            </Card>
          )}

          {tab === "Files" && (
            <Card title={`Collected files (${art.files.length})`}>
              <table>
                <thead><tr><th>Path</th><th>sha256</th><th>Size</th></tr></thead>
                <tbody>
                  {art.files.map((f) => (
                    <tr key={f.path}>
                      <td className="mono small">{f.path}</td>
                      <td className="mono small muted">{f.sha256}</td>
                      <td className="small">{(f.size / 1024).toFixed(1)} KiB</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="small muted mt">
                Secret-shaped content in scope fails closed as <code>409 artifact_secret</code> — nothing persisted.
              </div>
            </Card>
          )}

          {tab === "Tests & warnings" && (
            <Card title="test_command results + warnings">
              {art.tests.length ? (
                <table>
                  <thead><tr><th>Command</th><th>exit_code</th></tr></thead>
                  <tbody>
                    {art.tests.map((t) => (
                      <tr key={t.command}>
                        <td className="mono small">{t.command}</td>
                        <td><Badge tone={t.exit_code === 0 ? "green" : "red"}>{t.exit_code}</Badge></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : <div className="muted small">No test_command declared.</div>}
              {art.warnings.length > 0 && (
                <div className="mt">
                  {art.warnings.map((w) => <div key={w} className="alert amber">{w}</div>)}
                </div>
              )}
            </Card>
          )}

          {tab === "Structured output" && (
            producingRun?.structured_output != null ? (
              <Card title={`Run ${producingRun.id} — structured_output + output_contract`}>
                <div className="row" style={{ marginBottom: 10 }}>
                  <Badge tone={producingRun.output_contract?.status === "valid" ? "green" : "red"}>
                    contract: {producingRun.output_contract?.status ?? "n/a"}
                  </Badge>
                  {producingRun.output_contract?.enforcement && <Badge tone="gray">enforcement: {producingRun.output_contract.enforcement}</Badge>}
                  {producingRun.output_contract?.extraction && <Badge tone="blue">extracted: {producingRun.output_contract.extraction}</Badge>}
                </div>
                <JsonView value={producingRun.structured_output} />
                {producingRun.output_contract?.violations?.length ? (
                  <JsonView value={producingRun.output_contract.violations} />
                ) : null}
              </Card>
            ) : (
              <Empty title="No structured output" hint="Runs created with output_contract.schema get a control-plane verdict persisted on the terminal run." />
            )
          )}
        </div>
      </div>
    </div>
  );
}

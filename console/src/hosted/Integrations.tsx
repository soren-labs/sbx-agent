import { useEffect, useState, type FormEvent } from "react";
import { CodexConnection } from "./CodexConnection";
import { GitHubConnection } from "./GitHubConnection";
import { hostedRequest, type HostedConnection } from "./api";
import { Icon } from "../prototype/Icon";
import { Badge, ConnectionCard, notifyConnectionChange } from "./ui";

export function HostedIntegrations() {
  const [connection, setConnection] = useState<HostedConnection | null>(null);
  const [configured, setConfigured] = useState(false);
  const [mock, setMock] = useState(false);
  const [oauthConfigured, setOauthConfigured] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const refresh = async () => {
    const data = await hostedRequest("/hosted/connections/modal");
    setConnection(data.connection); setConfigured(data.configured); setMock(data.mock);
    setOauthConfigured(Boolean(data.oauth_configured ?? data.mock));
  };
  useEffect(() => { void refresh().catch((e) => setError(e.message)); }, []);
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => void refresh().catch(() => {}), 500);
    return () => clearInterval(timer);
  }, [busy]);
  const action = async (work: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await work(); } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); await refresh().catch(() => {}); notifyConnectionChange(); }
  };
  const provision = async () => {
    const data = await hostedRequest("/hosted/connections/modal/provision", {});
    setConnection(data.connection);
  };
  const connect = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    void action(async () => {
      await hostedRequest("/hosted/connections/modal", values);
      form.reset();
      await provision();
    });
  };
  const oauth = () => void action(async () => {
    const authorization = await hostedRequest("/hosted/connections/modal/authorize", {});
    if (authorization.mock) {
      await hostedRequest("/hosted/connections/modal/mock-approve", { state: authorization.state });
      await provision();
    } else window.location.assign(authorization.authorization_url);
  });
  const state = connection?.state;
  const ready = state === "ready";
  const failed = state === "failed" || state === "error";
  const tone = ready ? "ok" : failed ? "error" : busy || (state && state !== "not_connected") ? "busy" : "idle";
  const [showTokens, setShowTokens] = useState(false);
  return <div className="page-scroll"><div className="settings-content hs-page">
    <div className="page-eyebrow">YOUR CONNECTIONS</div>
    <h1>Integrations</h1>
    <p className="hs-lead">Three connections power every Session: compute on Modal, code on GitHub, and the agent on Codex. Credentials stay encrypted on the control plane.</p>
    {error && <p className="hs-alert" role="alert"><Icon name="x" size={13} />{error} <a href="/auth">Sign in</a></p>}
    <section className={`settings-section hs-card ${ready ? "is-ready" : failed ? "is-failed" : ""}`} aria-label="Modal connection">
      <ConnectionCard icon="cpu" step={1} title="Modal" description="Sandboxed compute where agents run, build and test."
        badge={<Badge tone={tone} role="status">{ready ? "Ready" : state?.replaceAll("_", " ") ?? "Not connected"}</Badge>}>
        {mock && <p className="fine-print">Mock workspace for Alpha development.</p>}
        {!configured && <p className="fine-print">Modal connection is not configured for this deployment.</p>}
        {failed && <p className="hs-note warn">Provisioning didn't finish. Reconcile the runtime, or reconnect with a fresh token.</p>}
        {connection?.metadata.progress && <ol className="hs-progress">{connection.metadata.progress.map((step: string) =>
          <li key={step}><Icon name="check" size={11} />{step}: complete</li>)}</ol>}
        {connection?.metadata.runtime_version && <p className="hs-meta">Runtime: <code>{connection.metadata.runtime_version}</code></p>}
        <div className="hs-actions">
          {oauthConfigured && <button className="button primary" disabled={busy || !configured} onClick={oauth}>Connect with Modal authorization</button>}
          {connection && <button className="button" disabled={busy} onClick={() => void action(provision)}><Icon name="refresh" size={13} />Reconcile runtime</button>}
          <button type="button" className="button ghost" aria-expanded={showTokens || !oauthConfigured} onClick={() => setShowTokens(v => !v)} hidden={!oauthConfigured}>Use API token instead</button>
        </div>
        <form className="hs-token-form" onSubmit={connect} hidden={oauthConfigured && !showTokens}>
          <label className="form-label">Modal Token ID<input name="token_id" type="password" autoComplete="off" required placeholder="ak-…" /></label>
          <label className="form-label">Modal Token Secret<input name="token_secret" type="password" autoComplete="off" required placeholder="as-…" /></label>
          <button className="button primary" disabled={busy || !configured}>Connect Modal</button>
        </form>
      </ConnectionCard>
    </section>
    <GitHubConnection />
    <CodexConnection />
  </div></div>;
}

import { useEffect, useState, type FormEvent } from "react";
import { ManualConnection } from "./ManualConnection";
import { CodexConnection } from "./CodexConnection";
import { GitHubConnection } from "./GitHubConnection";
import { hostedRequest, type HostedConnection } from "./api";

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
    finally { setBusy(false); await refresh().catch(() => {}); }
  };
  const provision = async () => {
    const data = await hostedRequest("/hosted/connections/modal/provision", {});
    setConnection(data.connection);
  };
  const connect = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    form.reset();
    void action(async () => {
      await hostedRequest("/hosted/connections/modal", values);
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
  return <div className="page-scroll"><div className="settings-content">
    <div className="page-eyebrow">YOUR CONNECTIONS</div>
    <h1>Integrations</h1>
    <p>Start with email and password, Modal tokens, an OpenCode Zen API key, and a GitHub token. ChatGPT/Codex is optional.</p>
    {error && <p role="alert">{error} <a href="/auth">Sign in</a></p>}
    <section className="settings-section">
      <h2>Modal</h2>
      <p role="status">{connection?.state === "ready" ? "Connected · Ready" : connection?.state === "failed" ? "Invalid — check or replace tokens" :
        connection?.state.replaceAll("_", " ") ?? "Not connected"}</p>
      {mock && <p className="fine-print">Mock workspace for Alpha development.</p>}
      {!configured && <p>Modal connection is not configured for this deployment.</p>}
      {connection?.metadata.progress && <ol>{connection.metadata.progress.map((step: string) =>
        <li key={step}>{step}: complete</li>)}</ol>}
      {connection?.metadata.runtime_version && <p>Runtime: {connection.metadata.runtime_version}</p>}
      <form onSubmit={connect}>
        <label className="form-label">Modal Token ID<input name="token_id" type="password" autoComplete="off" required /></label>
        <label className="form-label">Modal Token Secret<input name="token_secret" type="password" autoComplete="off" required /></label>
        <button disabled={busy || !configured}>{connection && connection.state !== "disabled" ? "Replace Modal" : "Connect Modal"}</button>
      </form>
      {oauthConfigured && <button disabled={busy || !configured} onClick={oauth}>Connect with Modal authorization</button>}
      {connection && connection.state !== "disabled" && <button disabled={busy} onClick={() => void action(async () => { await hostedRequest("/hosted/connections/modal", {}, "DELETE"); })}>Disconnect Modal</button>}
      {connection && connection.state !== "disabled" && <button disabled={busy} onClick={() => void action(provision)}>Reconcile runtime</button>}
    </section>
    <ManualConnection provider="opencode" title="OpenCode Zen" field="api_key" label="OpenCode Zen API Key" help="Use a Zen API key. Validation checks model access with a minimal inference request; free coding models are preferred. Only verified accessible models appear in new Sessions. The key stays encrypted on the server." />
    <GitHubConnection />
    <details><summary>Optional ChatGPT / Codex</summary><CodexConnection /></details>
  </div></div>;
}

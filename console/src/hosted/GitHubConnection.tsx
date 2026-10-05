import { useEffect, useState } from "react";
import { hostedRequest } from "./api";
import { Icon } from "../prototype/Icon";
import { Badge, ConnectionCard, notifyConnectionChange } from "./ui";

export function GitHubConnection() {
  const [installations, setInstallations] = useState<any[]>([]);
  const [configured, setConfigured] = useState(false);
  const [mock, setMock] = useState(false);
  const [operatorBinding, setOperatorBinding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const refresh = async () => {
    const status = await hostedRequest("/hosted/connections/github");
    setInstallations(status.installations); setConfigured(status.configured); setMock(status.mock);
    setOperatorBinding(status.binding_mode === "operator_approved");
  };
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const state = params.get("state"), installation = params.get("installation_id");
    const load = async () => {
      if (state && installation) {
        window.history.replaceState(null, "", "/integrations");
        await hostedRequest("/hosted/connections/github/callback", {state, installation_id: Number(installation)});
      }
      await refresh();
    };
    void load().catch(e => setError(e.message));
  }, []);
  const act = async (work: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await work(); await refresh(); notifyConnectionChange(); } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const connect = () => void act(async () => {
    const authorization = await hostedRequest("/hosted/connections/github/authorize", {});
    if (authorization.mock) await hostedRequest("/hosted/connections/github/mock-approve", {state: authorization.state});
    else window.location.assign(authorization.authorize_url);
  });
  const connected = installations.length > 0;
  return <section className={`settings-section hs-card ${connected ? "is-ready" : ""}`} aria-label="GitHub connection">
    <ConnectionCard icon="github" step={2} title="GitHub" description="Authorize repositories for your Sessions. GitHub access is independent of your compute workspace."
      badge={<Badge tone={connected ? "ok" : "idle"}>{connected ? `${installations.length} installation${installations.length > 1 ? "s" : ""}` : "Not connected"}</Badge>}>
      {error && <p className="hs-alert" role="alert">{error}</p>}
      {mock && <p className="fine-print">Mock GitHub installations for Alpha development.</p>}
      {operatorBinding && <p className="hs-note">Install SBX Agent on your repositories, then ask the deployment operator to approve the installation for your SBX account. Reload this page after approval.</p>}
      {!configured && <p className="fine-print">GitHub installation is not configured for this deployment.</p>}
      {installations.map(installation => <div className="hs-install" key={installation.installation_id}>
        <div className="hs-install-head">
          <span className="user-avatar avatar">{installation.account_login[0]?.toUpperCase()}</span>
          <p>Connected: {installation.account_login}</p>
          <small>{installation.repositories.length} repositories</small>
          <button className="button ghost danger" disabled={busy} onClick={() => void act(() => hostedRequest(`/hosted/connections/github/installations/${installation.installation_id}`, {}, "DELETE"))}>Disconnect {installation.account_login}</button>
        </div>
        <ul className="hs-repo-list">{installation.repositories.map((repo: string) => <li key={repo}><Icon name="branch" size={12} />{repo}</li>)}</ul>
      </div>)}
      <div className="hs-actions"><button className={`button ${connected ? "" : "primary"}`} disabled={busy || !configured} onClick={connect}><Icon name="plus" size={13} />Connect GitHub</button></div>
    </ConnectionCard>
  </section>;
}

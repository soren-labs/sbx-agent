import { useEffect, useState, type FormEvent } from "react";
import { hostedRequest, type HostedConnection } from "./api";
import { Icon } from "../prototype/Icon";
import { Badge, ConnectionCard, notifyConnectionChange } from "./ui";

type Props = { provider: "github" | "opencode"; title: string; field: "token" | "api_key"; label: string; help: string };

export function ManualConnection({ provider, title, field, label, help }: Props) {
  const [connection, setConnection] = useState<HostedConnection | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const path = `/hosted/connections/${provider}`;
  const refresh = async () => setConnection((await hostedRequest(path)).connection ?? null);
  useEffect(() => { void refresh().catch(e => setError(e.message)); }, [path]);
  const act = async (action: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await action(); notifyConnectionChange(); }
    catch (e) { setError((e as Error).message); }
    finally { await refresh().catch(() => {}); setBusy(false); }
  };
  const connect = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const value = new FormData(form).get(field);
    // Clear the input before awaiting the request; secrets never enter prefs/storage.
    form.reset();
    void act(() => hostedRequest(path, { [field]: value }));
  };
  const enabled = connection && connection.state !== "disabled";
  const state = connection?.state === "connected" ? "Connected" : connection?.state === "invalid" ? "Invalid" : "Disabled";
  return <section className={`settings-section hs-card ${state === "Connected" ? "is-ready" : state === "Invalid" ? "is-failed" : ""}`} aria-label={`${title} connection`}>
    <ConnectionCard icon={provider === "github" ? "github" : "sparkle"} step={provider === "github" ? 3 : 2} title={title} description={help}
      badge={<Badge tone={state === "Connected" ? "ok" : state === "Invalid" ? "error" : "idle"} role="status">{state}</Badge>}>
      {error && <p className="hs-alert" role="alert">{error}</p>}
      {connection?.metadata.error && <p className="hs-note warn">{String(connection.metadata.error).replaceAll("_", " ")}</p>}
      <form className="hs-token-form" onSubmit={connect}>
        <label className="form-label">{label}<input name={field} type="password" autoComplete="off" required disabled={busy} /></label>
        <button className="button primary" disabled={busy}>{enabled ? `Replace ${title}` : `Connect ${title}`}</button>
      </form>
      {enabled && <div className="hs-actions">
        <button className="button" disabled={busy} onClick={() => void act(() => hostedRequest(`${path}/validate`, {}))}><Icon name="refresh" size={13} />Validate {title}</button>
        <button className="button ghost danger" disabled={busy} onClick={() => void act(() => hostedRequest(path, {}, "DELETE"))}>Disconnect {title}</button>
      </div>}
      {connection?.metadata.models && <p className="hs-meta">Available models: {connection.metadata.models.join(" · ")}</p>}
      {connection?.metadata.repositories && <ul className="hs-repo-list">{connection.metadata.repositories.map((r: { name: string }) =>
        <li key={r.name}><Icon name="branch" size={12} />{r.name}</li>)}</ul>}
    </ConnectionCard>
  </section>;
}

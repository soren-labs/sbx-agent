import { useEffect, useState, type FormEvent } from "react";
import { hostedRequest, type HostedConnection } from "./api";

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
    try { await action(); window.dispatchEvent(new Event("sbx-connection-change")); }
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
  return <section className="settings-section" aria-label={`${title} connection`}>
    <h2>{title}</h2>
    <p>{help}</p>
    <p role="status">{state}</p>
    {error && <p role="alert">{error}</p>}
    {connection?.metadata.error && <p>{String(connection.metadata.error).replaceAll("_", " ")}</p>}
    <form onSubmit={connect}>
      <label className="form-label">{label}<input name={field} type="password" autoComplete="off" required disabled={busy} /></label>
      <button disabled={busy}>{enabled ? `Replace ${title}` : `Connect ${title}`}</button>
    </form>
    {enabled && <>
      <button disabled={busy} onClick={() => void act(() => hostedRequest(`${path}/validate`, {}))}>Validate {title}</button>
      <button disabled={busy} onClick={() => void act(() => hostedRequest(path, {}, "DELETE"))}>Disconnect {title}</button>
    </>}
    {connection?.metadata.models && <p>Available models: {connection.metadata.models.join(" · ")}</p>}
    {connection?.metadata.repositories && <p>Repositories: {connection.metadata.repositories.map((r: { name: string }) => r.name).join(" · ")}</p>}
  </section>;
}

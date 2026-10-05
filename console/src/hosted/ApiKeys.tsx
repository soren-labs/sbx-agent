import { useEffect, useState } from "react";
import { hostedRequest } from "./api";
import { Icon } from "../prototype/Icon";

export function ApiKeys() {
  const [keys, setKeys] = useState<any[]>([]);
  const [label, setLabel] = useState("");
  const [days, setDays] = useState("90");
  const [plaintext, setPlaintext] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const load = async () => setKeys((await hostedRequest("/hosted/api-keys")).api_keys);
  useEffect(() => {void load().catch(e => setError(e.message));}, []);
  const create = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError(""); setPlaintext("");
    try {
      const result = await hostedRequest("/hosted/api-keys", {label, scopes: ["agents"], expires_in_days: days ? Number(days) : null});
      setPlaintext(result.key); setLabel(""); await load();
    } catch(e) {setError((e as Error).message);} finally {setBusy(false);}
  };
  const revoke = async (id: string) => {
    setBusy(true); setError("");
    try {await hostedRequest(`/hosted/api-keys/${id}`, {}, "DELETE"); await load(); setPlaintext("");}
    catch(e) {setError((e as Error).message);} finally {setBusy(false);}
  };
  const copy = () => void navigator.clipboard?.writeText(plaintext).then(() => {setCopied(true); setTimeout(() => setCopied(false), 1600);});
  const active = keys.filter(k => !k.revoked_at), revoked = keys.filter(k => k.revoked_at);
  return <section className="settings-section hs-card" aria-label="API Keys">
    <h2 className="hs-section-title">API Keys</h2>
    <p className="hs-muted">Personal keys use your connected Modal, GitHub and Codex accounts.</p>
    {error && <p className="hs-alert" role="alert">{error}</p>}
    <form className="api-key-form hs-key-form" onSubmit={event => void create(event)}>
      <label className="form-label">Key name<input aria-label="Key name" value={label} onChange={e => setLabel(e.target.value)} maxLength={80} placeholder="e.g. CI pipeline"/></label>
      <label className="form-label">Scope<select aria-label="Key scope" disabled><option>Sessions (agents)</option></select></label>
      <label className="form-label">Expiry<select aria-label="Key expiry" value={days} onChange={e => setDays(e.target.value)}><option value="30">30 days</option><option value="90">90 days</option><option value="365">365 days</option><option value="">No expiry</option></select></label>
      <button className="button primary" disabled={busy}><Icon name="plus" size={13} />Create API key</button>
    </form>
    {plaintext && <div className="hs-secret"><p><Icon name="shield" size={13} />Save this key now. It is shown only once.</p>
      <div className="hs-secret-row"><input aria-label="New API key" readOnly value={plaintext} autoComplete="off" spellCheck={false} onFocus={e => e.currentTarget.select()}/>
        <button type="button" className="button" onClick={copy}><Icon name={copied ? "check" : "copy"} size={13} />{copied ? "Copied" : "Copy"}</button>
        <button className="button ghost" onClick={() => setPlaintext("")}>Dismiss key</button></div></div>}
    {keys.length === 0 ? <div className="hs-empty"><Icon name="key" size={18} /><p>No API keys yet. Create one to start Sessions from scripts or CI.</p></div> :
      <ul className="hs-key-list">{[...active, ...revoked].map(key => <li className={key.revoked_at ? "revoked" : ""} key={key.id}>
        <Icon name="key" size={14} />
        <div><strong>{key.label || "Unnamed key"}</strong><p>{key.scopes.join(", ")} · {key.revoked_at ? "Revoked" : key.expires_at ? `Expires ${new Date(key.expires_at).toLocaleDateString()}` : "No expiry"}</p></div>
        <button className="button ghost danger" disabled={busy || Boolean(key.revoked_at)} onClick={() => void revoke(key.id)}>Revoke {key.label || "key"}</button>
      </li>)}</ul>}
  </section>;
}

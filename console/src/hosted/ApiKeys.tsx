import { useEffect, useState } from "react";
import { hostedRequest } from "./api";

export function ApiKeys() {
  const [keys, setKeys] = useState<any[]>([]);
  const [label, setLabel] = useState("");
  const [days, setDays] = useState("90");
  const [plaintext, setPlaintext] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
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
  return <section className="settings-section"><h2>API Keys</h2><p>Personal keys use your connected Modal, GitHub and Codex accounts.</p>
    {error && <p role="alert">{error}</p>}
    <form className="api-key-form" onSubmit={event => void create(event)}>
      <label className="form-label">Key name<input aria-label="Key name" value={label} onChange={e => setLabel(e.target.value)} maxLength={80}/></label>
      <label className="form-label">Scope<select aria-label="Key scope" disabled><option>Sessions (agents)</option></select></label>
      <label className="form-label">Expiry<select aria-label="Key expiry" value={days} onChange={e => setDays(e.target.value)}><option value="30">30 days</option><option value="90">90 days</option><option value="365">365 days</option><option value="">No expiry</option></select></label>
      <button className="button primary" disabled={busy}>Create API key</button>
    </form>
    {plaintext && <div className="merge-confirm"><p>Save this key now. It is shown only once.</p><input aria-label="New API key" readOnly value={plaintext} autoComplete="off" spellCheck={false}/><button className="button" onClick={() => setPlaintext("")}>Dismiss key</button></div>}
    {keys.map(key => <div className="review-section" key={key.id}><strong>{key.label || "Unnamed key"}</strong><p>{key.scopes.join(", ")} · {key.revoked_at ? "Revoked" : key.expires_at ? `Expires ${new Date(key.expires_at).toLocaleDateString()}` : "No expiry"}</p><button className="button" disabled={busy || Boolean(key.revoked_at)} onClick={() => void revoke(key.id)}>Revoke {key.label || "key"}</button></div>)}
  </section>;
}

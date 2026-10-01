import type { Session } from "../api/types";
const PREFIX = "sbx.history.v1.";
const TTL = 30 * 60_000;
const LIMIT = 8;
/** Credential digest scopes cached transcripts; credentials never enter storage. */
export async function cacheScope(base: string, token: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(base+"\0"+token));
  return Array.from(new Uint8Array(digest), b=>b.toString(16).padStart(2,"0")).join("");
}
export function readSessionCache(scope: string, id: string): Session | null {
  try {
    const raw = sessionStorage.getItem(PREFIX+scope+"."+id);
    const row = raw ? JSON.parse(raw) : null;
    return row && Date.now()-row.at < TTL && row.session?.id===id ? row.session : null;
  } catch { return null; }
}
export function writeSessionCache(scope: string, session: Session) {
  try {
    const value = JSON.stringify({at:Date.now(),session});
    if (value.length>500_000) return;
    const key = PREFIX+scope+"."+session.id;
    sessionStorage.setItem(key,value);
    const keys=Array.from({length:sessionStorage.length},(_,i)=>sessionStorage.key(i)!)
      .filter(k=>k.startsWith(PREFIX)).sort((a,b)=>{
        const at=(k:string)=>JSON.parse(sessionStorage.getItem(k)!).at;
        return at(b)-at(a);
      });
    keys.slice(LIMIT).forEach(k=>sessionStorage.removeItem(k));
  } catch { /* Storage denied/full: network is still authoritative. */ }
}

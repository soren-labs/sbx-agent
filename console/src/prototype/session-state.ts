import type { ActivityItem, Session, Turn } from "../api/types";

/** Detail snapshots omit activity; a snapshot must never erase streamed evidence. */
export function mergeActivity(current: ActivityItem[], incoming: ActivityItem[]): ActivityItem[] {
  const rows = new Map(current.map(item => [item.id, item]));
  for (const item of incoming) {
    const prior = rows.get(item.id);
    rows.set(item.id, prior ? {...prior, ...item, seq: prior.seq, ts: prior.ts} : item);
  }
  return [...rows.values()].sort((a,b) => a.seq-b.seq);
}
export function mergeTurn(current: Turn | undefined, incoming: Turn): Turn {
  if (!current) return incoming;
  return {...current, ...incoming,
    prompt: incoming.prompt || current.prompt,
    createdAt: incoming.createdAt || current.createdAt,
    startedAt: incoming.startedAt || current.startedAt,
    finishedAt: incoming.finishedAt ?? current.finishedAt,
    status: ["finished","failed","cancelled"].includes(current.status) && ["queued","running"].includes(incoming.status) ? current.status : incoming.status,
    result: incoming.result ?? current.result,
    activity: mergeActivity(current.activity, incoming.activity),
  };
}
export function mergeSession(current: Session | null, incoming: Session): Session {
  if (!current || current.id !== incoming.id) return incoming;
  const stale = new Date(incoming.updatedAt).getTime() < new Date(current.updatedAt).getTime();
  const turns = new Map(current.turns.map(turn => [turn.id, turn]));
  for (const turn of incoming.turns) { const prior=turns.get(turn.id);turns.set(turn.id,stale && prior ? {...prior,activity:mergeActivity(prior.activity,turn.activity)} : mergeTurn(prior,turn)); }
  return {...current, ...(stale ? {} : incoming), turns:[...turns.values()].sort((a,b)=>a.index-b.index)};
}

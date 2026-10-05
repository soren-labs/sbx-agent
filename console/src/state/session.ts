import type { Event, Session } from "../api/types";

export type State = {
  snapshot: Session;
  watermark: number;
  events: Event[];
  resetRequired: boolean;
};
export function initial(snapshot: Session): State {
  return {
    snapshot,
    watermark: snapshot.event_watermark,
    events: [],
    resetRequired: false,
  };
}
export function apply(state: State, event: Event): State {
  if (event.seq <= state.watermark) return state;
  if (event.schema_version !== 1 || event.seq !== state.watermark + 1)
    return { ...state, resetRequired: true };
  // Server snapshots supply projections; event feed supplies activity and invalidation only.
  return {
    ...state,
    watermark: event.seq,
    events: [...state.events, event].slice(-200),
  };
}
export class Cache {
  private values = new Map<string, State>();
  private owner = "";
  setOwner(owner: string) {
    if (owner !== this.owner) {
      this.values.clear();
      this.owner = owner;
    }
  }
  set(id: string, value: State) {
    if (this.values.size >= 32)
      this.values.delete(this.values.keys().next().value!);
    this.values.set(id, value);
  }
  get(id: string) {
    return this.values.get(id);
  }
  clear() {
    this.values.clear();
    this.owner = "";
  }
}
export const cache = new Cache();

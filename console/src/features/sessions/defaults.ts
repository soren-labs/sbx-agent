import type { Connection, ExecutorBackend, ModelsView } from "../../api/types";

/** Preferred FREE model: server preference first, ready connections before others. */
export function pickDefaultModel(v: ModelsView | undefined): string | undefined {
  if (!v) return undefined;
  const free = (c: ModelsView["connections"][number]) => c.models.filter((m) => m.free).map((m) => m.id);
  const ordered = [...v.connections].sort((a, b) => Number(b.health === "ready") - Number(a.health === "ready"));
  const allFree = new Set(ordered.flatMap(free));
  if (v.preferred_model && allFree.has(v.preferred_model)) return v.preferred_model;
  for (const c of ordered) {
    if (c.preferred_model && free(c).includes(c.preferred_model)) return c.preferred_model;
  }
  const first = ordered.flatMap(free)[0];
  return first ?? v.preferred_model ?? undefined;
}

export interface ModelOption {
  id: string;
  free: boolean;
}

export function modelOptions(v: ModelsView | undefined): ModelOption[] {
  const out = new Map<string, boolean>();
  for (const c of v?.connections ?? []) {
    for (const m of c.models) out.set(m.id, (out.get(m.id) ?? false) || Boolean(m.free));
  }
  return [...out].map(([id, free]) => ({ id, free }));
}

/** Modal when a Modal connection exists; otherwise Local. */
export function pickBackend(connections: Connection[] | undefined, backends?: ExecutorBackend[]): "modal" | "local" {
  const hasModal = (connections ?? []).some((c) => c.kind === "modal" && c.state === "configured");
  if (hasModal) return "modal";
  if (backends && !backends.some((b) => b.kind === "local") && backends.some((b) => b.kind === "modal")) return "modal";
  return "local";
}

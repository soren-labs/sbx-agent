import type { Connection, ExecutorBackend, Harness, ModelsView } from "../../api/types";
import { selectable } from "./harnesses";

type ModelConnection = ModelsView["connections"][number];

/** Connections the selected Harness can actually use, healthy ones first. */
export function usableConnections(v: ModelsView | undefined): ModelConnection[] {
  return (v?.connections ?? [])
    .filter((c) => c.compatible !== false && c.health !== "reauth_required")
    .sort((a, b) => Number(b.health === "ready") - Number(a.health === "ready"));
}

/** The model the server would pin by default for this Harness (the Connection's default). */
export function pickDefaultModel(v: ModelsView | undefined, connectionId?: string): string | undefined {
  const usable = usableConnections(v);
  const chosen = connectionId ? usable.find((c) => c.connection_id === connectionId) : usable[0];
  return chosen?.preferred_model ?? chosen?.models[0]?.id ?? undefined;
}

/** Distinct model ids offered by the usable Connections (or only the chosen one). */
export function modelOptions(v: ModelsView | undefined, connectionId?: string): string[] {
  const out = new Set<string>();
  for (const c of usableConnections(v)) {
    if (connectionId && c.connection_id !== connectionId) continue;
    if (c.preferred_model) out.add(c.preferred_model);
    for (const m of c.models) out.add(m.id);
  }
  return [...out];
}

/** First Harness some configured inference Connection can drive; OpenCode when none can. */
export function pickHarness(harnesses: Harness[] | undefined, connections: Connection[] | undefined): string {
  const offered = new Set(
    (connections ?? [])
      .filter((c) => c.kind === "inference_api" && c.state === "configured" && c.health !== "reauth_required")
      .flatMap((c) => Object.keys(c.config?.endpoints ?? {})),
  );
  const match = selectable(harnesses).find((h) => (h.inference_protocols ?? []).some((p) => offered.has(p)));
  return match?.provider_id ?? "opencode";
}

/** Modal when a Modal connection exists; otherwise Local. */
export function pickBackend(connections: Connection[] | undefined, backends?: ExecutorBackend[]): "modal" | "local" {
  const hasModal = (connections ?? []).some((c) => c.kind === "modal" && c.state === "configured");
  if (hasModal) return "modal";
  if (backends && !backends.some((b) => b.kind === "local") && backends.some((b) => b.kind === "modal")) return "modal";
  return "local";
}

/** Repositories the user is known to reach: GitHub validation results and Project specs. */
export function knownRepositories(connections: Connection[] | undefined, projectRepos: string[]): string[] {
  const out = new Set<string>(projectRepos);
  for (const c of connections ?? []) {
    if (c.kind !== "github" || c.state !== "configured") continue;
    const repos = (c.validation?.details?.repositories ?? {}) as Record<string, { visible?: boolean }>;
    for (const [name, access] of Object.entries(repos)) if (access.visible) out.add(name);
    for (const name of (c.validation?.details?.installation_repositories ?? []) as string[]) out.add(name);
  }
  return [...out].sort();
}

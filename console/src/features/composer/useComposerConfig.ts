import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAuth } from "../../state/auth";
import { useApi } from "../../state/context";
import { useQuery } from "../../state/query";
import type { CreateSessionBody } from "../../api/types";
import { useConnections } from "../connections/useConnections";
import { useMachineSlots } from "../machines/useMachineSlots";
import { knownRepositories, modelOptions, pickBackend, pickDefaultModel, pickHarness, usableConnections } from "../sessions/defaults";
import { selectable } from "../sessions/harnesses";

/**
 * All Session-source selection state and the defaults the server data implies:
 * source (API key vs Machine Slot), harness, model, effort, executor backend,
 * project/repository. Ported 1:1 from the previous NewSession form.
 */
export function useComposerConfig() {
  const api = useApi();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const connections = useConnections();
  const harnesses = useQuery(["harnesses"], () => api.catalog.harnesses());
  const backends = useQuery(["executor-backends"], () => api.catalog.executorBackends());
  const projects = useQuery(w ? ["projects", w] : null, () => api.projects.list(w!));
  const machines = useMachineSlots();

  // Projects deep-links a new Session at `/?project=<id>`; the pick applies once on mount.
  const [params] = useSearchParams();
  const [projectId, setProjectId] = useState(() => params.get("project") ?? "");
  const [repo, setRepo] = useState("");
  const [baseRef, setBaseRef] = useState("main");
  const [harnessOverride, setHarnessOverride] = useState<string | null>(null);
  const [connectionId, setConnectionId] = useState("");
  const [modelOverride, setModelOverride] = useState<string | null>(null);
  const [backendOverride, setBackendOverride] = useState<string | null>(null);
  // "" = custom API key; otherwise the Machine Slot (subscription login) to run on.
  const [sourceOverride, setSourceOverride] = useState<string | null>(null);
  const [effortOverride, setEffortOverride] = useState<string | null>(null);

  const offered = selectable(harnesses.data?.items);
  // A Harness without a custom-API mode runs on a machine only; it is not a choice for an API key.
  const apiOffered = offered.filter((h) => (h.inference_protocols ?? []).length > 0);
  const apiHarness = harnessOverride ?? pickHarness(harnesses.data?.items, connections.data?.items);
  const slots = machines.data?.items ?? [];
  const hasApiKey = (connections.data?.items ?? []).some(
    (c) => c.kind === "inference_api" && c.state === "configured" && c.health !== "reauth_required",
  );
  // Only machines that can run (or are running) are offered; the rest are one link away.
  const usableSlots = slots.filter((m) => m.status === "ready" || m.status === "running");
  const idleSlots = slots.length - usableSlots.length;
  const firstFree = slots.find((m) => m.status === "ready");
  // With no API key at all, a ready machine is the only way to run: preselect it.
  const sourceId = sourceOverride ?? (!hasApiKey && connections.data && firstFree ? firstFree.id : "");
  const slot = slots.find((m) => m.id === sourceId && (m.status === "ready" || m.status === "running")) ?? null;
  const slotHarness = slot
    ? ((machines.data?.providers ?? []).find((p) => p.provider_id === slot.provider)?.harness_provider_id ?? slot.provider)
    : null;
  const harness = slotHarness ?? apiHarness;
  const manifest = offered.find((h) => h.provider_id === harness);
  // Models and compatibility are scoped to the selected Harness: the same key may
  // serve one CLI and not another, depending on the protocols it offers.
  const models = useQuery(w ? ["models", w, harness] : null, () => api.catalog.models(w!, harness));
  const usable = usableConnections(models.data);
  const pinned = usable.some((c) => c.connection_id === connectionId) ? connectionId : "";
  // A machine's models are only what its authenticated CLI reported; nothing is built in.
  const catalog = slot?.capabilities.catalog?.status === "ready" ? slot.capabilities.catalog : null;
  const slotModels = catalog?.models ?? [];
  const options = slot ? slotModels.map((m) => m.id) : modelOptions(models.data, pinned || undefined);
  const model = slot
    ? modelOverride && options.includes(modelOverride)
      ? modelOverride
      : (catalog?.default_model ?? "")
    : (modelOverride ?? pickDefaultModel(models.data, pinned || undefined) ?? "");
  const apiModel = (pinned ? usable.filter((c) => c.connection_id === pinned) : usable)
    .flatMap((c) => c.models)
    .find((m) => m.id === model);
  // Reasoning values this exact model supports here: a machine model's own levels, or a
  // custom API model's verified thinking switch. Empty means there is nothing to choose.
  const efforts = slot
    ? (slotModels.find((m) => m.id === model)?.reasoning.efforts ?? [])
    : (apiModel?.efforts ?? []).map((id) => ({ id, description: null as string | null }));
  const effort = effortOverride && efforts.some((e) => e.id === effortOverride) ? effortOverride : "";
  const thinkingToggle = !slot && efforts.length === 1 && efforts[0].id === "none";
  const backendKinds = (backends.data?.items ?? []).map((b) => b.kind);
  const backend = slot ? "modal" : (backendOverride ?? pickBackend(connections.data?.items, backends.data?.items));
  const repos = knownRepositories(
    connections.data?.items,
    (projects.data?.items ?? []).flatMap((p) => (p.current_version ? [p.current_version.spec.repository.full_name] : [])),
  );
  const blocked = !slot && models.data !== undefined && usable.length === 0;
  // Sending before the source data arrives picks defaults (e.g. backend "local")
  // that a moment later would have been different: gate the submit on the load.
  const ready =
    connections.data !== undefined &&
    harnesses.data !== undefined &&
    backends.data !== undefined &&
    projects.data !== undefined &&
    (slot !== null || models.data !== undefined);

  const chooseHarness = (next: string) => {
    setHarnessOverride(next);
    // The model list belongs to the Harness's compatible Connections.
    setModelOverride(null);
    setEffortOverride(null);
    setConnectionId("");
  };

  const chooseSource = (next: string) => {
    setSourceOverride(next);
    // Models and efforts belong to the chosen machine (or API key): never carry them over.
    setModelOverride(null);
    setEffortOverride(null);
  };

  const chooseModel = (next: string | null) => {
    setModelOverride(next);
    setEffortOverride(null);
  };

  const chooseConnection = (next: string) => {
    setConnectionId(next);
    setModelOverride(null);
  };

  const buildCreateBody = (prompt: string): CreateSessionBody => {
    // The first line of the task names the Session, so lists never show "Untitled".
    const firstLine = prompt.trim().split("\n")[0].trim();
    return {
      ...(firstLine ? { title: firstLine.length > 80 ? `${firstLine.slice(0, 79)}…` : firstLine } : {}),
      harness: { provider_id: harness, ...(model ? { model } : {}), ...(effort ? { effort } : {}) },
      executor: { backend },
      ...(slot
        ? { inference: { mode: "subscription" as const, machine_slot_id: slot.id } }
        : pinned
          ? { connections: { inference: pinned } }
          : {}),
      ...(projectId
        ? { project_id: projectId }
        : repo.trim()
          ? { repository: { full_name: repo.trim(), base_ref: baseRef.trim() || "main" } }
          : {}),
      ...(prompt.trim() ? { message: { content: prompt.trim() } } : {}),
    };
  };

  return {
    w,
    connections,
    projects,
    offered,
    apiOffered,
    slots,
    usableSlots,
    idleSlots,
    hasApiKey,
    slot,
    catalog,
    slotModels,
    harness,
    manifest,
    model,
    modelsQuery: models,
    usable,
    pinned,
    options,
    efforts,
    effort,
    thinkingToggle,
    backend,
    backendKinds,
    repos,
    blocked,
    ready,
    projectId,
    repo,
    baseRef,
    modelOverride,
    chooseHarness,
    chooseSource,
    chooseModel,
    chooseConnection,
    setEffortOverride,
    setBackendOverride,
    setProjectId,
    setRepo,
    setBaseRef,
    buildCreateBody,
  };
}

export type ComposerConfig = ReturnType<typeof useComposerConfig>;

import type { ProjectCheck, ProjectService, ProjectSpec } from "../../api/types";
import { joinArgv } from "./argv";

/** The fields the Project form edits; everything else in the spec is carried over. */
export interface ProjectEdits {
  repository: string;
  baseRef: string;
  checks: { name: string; argv: string[] }[];
  services: { name: string; argv: string[]; port: number | null }[];
  harness: string;
  model: string;
  backend: string;
}

const clone = <T>(v: T): T => (v === undefined ? v : (JSON.parse(JSON.stringify(v)) as T));

/** Form values for a spec (a new Project starts from these defaults). */
export function editsOf(spec: ProjectSpec | undefined, fallbackHarness = "opencode"): ProjectEdits {
  return {
    repository: spec?.repository?.full_name ?? "",
    baseRef: spec?.repository?.base_ref ?? "main",
    checks: (spec?.checks ?? []).map((c) => ({ name: c.name, argv: [...c.argv] })),
    services: (spec?.services ?? []).map((s) => ({ name: s.name, argv: [...s.argv], port: s.port ?? null })),
    harness: spec?.defaults?.harness?.provider_id ?? fallbackHarness,
    model: spec?.defaults?.harness?.model ?? "",
    backend: spec?.defaults?.executor?.backend ?? "modal",
  };
}

/** Reuse each previous entry with the same name, so fields the form does not edit survive. */
function carry<T extends { name: string }>(prev: T[] | undefined, next: (Partial<T> & { name: string })[]): T[] {
  const pool = [...(prev ?? [])];
  return next.map((n) => {
    const i = pool.findIndex((p) => p.name === n.name);
    const base = i >= 0 ? pool.splice(i, 1)[0] : undefined;
    return { ...clone(base), ...n } as T;
  });
}

/**
 * The next complete spec: a deep copy of the current one with only the edited fields
 * replaced. Unknown and hidden fields (environment, ship policy, effort, service health,
 * check timeouts, anything newer than this Console) are kept as they are.
 */
export function deriveSpec(current: ProjectSpec | undefined, e: ProjectEdits): ProjectSpec {
  const next = (clone(current) ?? { repository: null }) as ProjectSpec;
  const prevRepo = current?.repository ?? null;
  const full = e.repository.trim();
  const baseRef = e.baseRef.trim() || "main";
  if (!full) next.repository = null;
  else if (!prevRepo || prevRepo.full_name !== full || prevRepo.base_ref !== baseRef) {
    const repo: Record<string, unknown> = { ...clone(prevRepo ?? {}), full_name: full, base_ref: baseRef };
    // The server derives clone_url from the name; a stale one must not travel along.
    if (prevRepo?.full_name !== full) delete repo.clone_url;
    next.repository = repo as ProjectSpec["repository"];
    // A delivery base that followed the repository base keeps following it (shown in the diff).
    const ship = next.ship_policy;
    if (ship && prevRepo && ship.base_branch === prevRepo.base_ref) ship.base_branch = baseRef;
  }
  next.checks = carry<ProjectCheck>(current?.checks, e.checks);
  next.services = carry<ProjectService>(
    current?.services,
    e.services.map((s) => ({ name: s.name, argv: s.argv, port: s.port })),
  );
  const defaults = { ...(next.defaults ?? {}) };
  const prevHarness = current?.defaults?.harness;
  const model = e.model.trim();
  // Effort and other Harness options belong to that CLI: switching CLI drops them visibly.
  const harness: Record<string, unknown> =
    prevHarness && prevHarness.provider_id === e.harness ? { ...clone(prevHarness) } : { provider_id: e.harness };
  if (model) harness.model = model;
  else delete harness.model;
  defaults.harness = harness as NonNullable<ProjectSpec["defaults"]>["harness"];
  if (defaults.executor?.backend !== e.backend) defaults.executor = { ...(defaults.executor ?? {}), backend: e.backend };
  next.defaults = defaults;
  return next;
}

export interface SpecChange {
  path: string;
  before: string | null;
  after: string | null;
  /** Delivery policy decides what may ship and merge: never changed quietly. */
  policy: boolean;
}

const isArgv = (path: string) => /(^|\.)(checks|services)\[[^\]]+\]\.argv$/.test(path);

function leaves(value: unknown, path: string, out: Map<string, string>): void {
  if (Array.isArray(value)) {
    if (isArgv(path) && value.every((v) => typeof v === "string")) {
      out.set(path, joinArgv(value as string[]));
      return;
    }
    if (!value.length) {
      out.set(path, "[]");
      return;
    }
    value.forEach((v, i) => {
      const key = v && typeof v === "object" && typeof (v as { name?: unknown }).name === "string" ? (v as { name: string }).name : String(i);
      leaves(v, `${path}[${key}]`, out);
    });
    return;
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (!entries.length) {
      out.set(path, "{}");
      return;
    }
    for (const [k, v] of entries) leaves(v, path ? `${path}.${k}` : k, out);
    return;
  }
  out.set(path, JSON.stringify(value ?? null));
}

// Server-derived values: equal by construction, so they are not changes the user made.
const DERIVED = new Set(["repository.clone_url", "repository.provider", "schema_version"]);

/** Field-level differences between two specs, delivery-policy changes first. */
export function specChanges(before: ProjectSpec | undefined, after: ProjectSpec): SpecChange[] {
  const a = new Map<string, string>();
  const b = new Map<string, string>();
  if (before) leaves(before, "", a);
  leaves(after, "", b);
  const out: SpecChange[] = [];
  for (const path of new Set([...a.keys(), ...b.keys()])) {
    if (DERIVED.has(path)) continue;
    const x = a.get(path) ?? null;
    const y = b.get(path) ?? null;
    if (x === y) continue;
    out.push({ path, before: x, after: y, policy: path.startsWith("ship_policy") });
  }
  return out.sort((p, q) => Number(q.policy) - Number(p.policy) || p.path.localeCompare(q.path));
}

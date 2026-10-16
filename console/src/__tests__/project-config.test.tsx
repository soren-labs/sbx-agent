import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { ChangeSet, DeliveryCapability, Project, ProjectSpec } from "../api/types";
import { ProjectsPage } from "../features/projects/ProjectsPage";
import { deriveSpec, editsOf, specChanges } from "../features/projects/spec";
import { ChangesTab } from "../features/sessions/tabs/ChangesTab";
import { ServicesTab } from "../features/sessions/tabs/ServicesTab";
import { identityRoutes, mockFetch, renderApp } from "../test/helpers";

/** A stored spec with every section populated, as normalize_spec returns it. */
const FULL: ProjectSpec = {
  schema_version: 1,
  repository: { provider: "github", full_name: "acme/demo", base_ref: "develop", clone_url: "https://github.com/acme/demo.git", mirror: "internal" },
  environment: { env: { APP_MODE: "test" }, setup_hint: "make deps" },
  checks: [{ name: "unit", argv: ["python3", "-c", "print('ok')"], timeout_seconds: 120, label: "fast" }],
  services: [
    {
      name: "web",
      argv: ["python3", "-m", "http.server", "18950"],
      cwd: "app",
      port: 18950,
      health: { path: "/healthz" },
      restart: "never",
      order: 2,
      preview: true,
      owner: "frontend",
    },
  ],
  defaults: {
    harness: { provider_id: "claude", model: "claude-sonnet", effort: "high" },
    executor: { backend: "local", resource_class: "large" },
    turn_defaults: { max_minutes: 30 },
    budget: { max_turns: 5 },
  },
  ship_policy: {
    transport: "git_branch",
    base_branch: "release",
    draft: false,
    automatic_delivery: false,
    required_results: [{ kind: "ReviewAssessment", count: 2, verdict: "approve" }],
    required_checks: ["ci/unit"],
    independence: "distinct_child_session",
    merge_methods: ["merge"],
    require_base_unchanged: true,
  },
  x_team_notes: { owner: "platform" },
};

const project = (spec: ProjectSpec = FULL): Project => ({
  id: "p1",
  workspace_id: "w1",
  slug: "full",
  name: "Full",
  version: 3,
  current_version: { id: "pv1", project_id: "p1", ordinal: 2, spec, spec_digest: "sha256:x", created_at: "2026-10-15T00:00:00Z" },
  created_at: "2026-10-15T00:00:00Z",
  updated_at: "2026-10-15T00:00:00Z",
});

describe("deriving a ProjectVersion from the current one (AUD03)", () => {
  it("changes only the edited field and keeps hidden and unknown fields", () => {
    const next = deriveSpec(FULL, { ...editsOf(FULL), model: "claude-opus" });
    expect(specChanges(FULL, next)).toEqual([
      { path: "defaults.harness.model", before: '"claude-sonnet"', after: '"claude-opus"', policy: false },
    ]);
    expect(next).toEqual({ ...FULL, defaults: { ...FULL.defaults, harness: { ...FULL.defaults!.harness, model: "claude-opus" } } });
  });

  it("is the identity when nothing is edited", () => {
    expect(deriveSpec(FULL, editsOf(FULL))).toEqual(FULL);
  });

  it("drops a CLI-specific effort visibly when the CLI changes", () => {
    const changes = specChanges(FULL, deriveSpec(FULL, { ...editsOf(FULL), harness: "codex" }));
    expect(changes.map((c) => [c.path, c.after])).toEqual([
      ["defaults.harness.effort", null],
      ["defaults.harness.provider_id", '"codex"'],
    ]);
  });

  it("keeps a delivery base that followed the repository base in step, and reports it as a policy change", () => {
    const following = { ...FULL, ship_policy: { ...FULL.ship_policy, base_branch: "develop" } };
    const changes = specChanges(following, deriveSpec(following, { ...editsOf(following), baseRef: "main" }));
    expect(changes[0]).toEqual({ path: "ship_policy.base_branch", before: '"develop"', after: '"main"', policy: true });
    // An explicitly different delivery base is left alone.
    expect(specChanges(FULL, deriveSpec(FULL, { ...editsOf(FULL), baseRef: "main" })).some((c) => c.policy)).toBe(false);
  });

  it("carries a service's undisplayed fields across an edit of its command", () => {
    const e = editsOf(FULL);
    const next = deriveSpec(FULL, { ...e, services: [{ ...e.services[0], argv: ["node", "server.js"] }] });
    expect(next.services).toEqual([{ ...FULL.services![0], argv: ["node", "server.js"] }]);
  });
});

function projectRoutes(p: Project, onPublish?: (body: unknown) => void) {
  return [
    ...identityRoutes,
    ["GET", "/api/workspaces/w1/projects", { json: { items: [p] } }],
    ["GET", "/api/harnesses", { json: { items: [{ provider_id: "opencode", support_tier: "supported", capabilities: {} }, { provider_id: "claude", support_tier: "supported", capabilities: {} }] } }],
    [
      "POST",
      "/api/projects/p1/versions",
      (call: { body: unknown }) => {
        onPublish?.(call.body);
        return { status: 201, json: p };
      },
    ],
  ] as Parameters<typeof mockFetch>[0];
}

async function openPublish() {
  await userEvent.click(await screen.findByRole("button", { name: "Project actions" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Publish new version" }));
  return screen.findByRole("form", { name: "Publish new version" });
}

describe("Projects page", () => {
  it("publishes the complete current spec with only the edited Model changed", async () => {
    let sent: { spec: ProjectSpec; expected_version: number } | null = null;
    const m = mockFetch(projectRoutes(project(), (b) => (sent = b as typeof sent)));
    await renderApp(<ProjectsPage />, m.fetch);
    const form = await openPublish();
    const publish = within(form).getByRole("button", { name: "Publish version" });
    // Hidden settings are shown as kept, and nothing is published without a change.
    expect(within(form).getByText("Reasoning effort: high")).toBeInTheDocument();
    expect(within(form).getByText("Delivery: ready-for-review branch to release")).toBeInTheDocument();
    expect(publish).toBeDisabled();
    const model = within(form).getByLabelText("Model");
    await userEvent.clear(model);
    await userEvent.type(model, "claude-opus");
    const changes = within(form).getByRole("list", { name: "Changes in this version" });
    expect(within(changes).getAllByRole("listitem")).toHaveLength(1);
    expect(changes).toHaveTextContent("defaults.harness.model");
    await userEvent.click(publish);
    await waitFor(() => expect(sent).not.toBeNull());
    expect(sent!.expected_version).toBe(3);
    expect(sent!.spec).toEqual({ ...FULL, defaults: { ...FULL.defaults, harness: { provider_id: "claude", model: "claude-opus", effort: "high" } } });
  });

  it("shows the exact argv of a check and refuses a command it cannot split (AUD06)", async () => {
    const m = mockFetch(projectRoutes(project()));
    await renderApp(<ProjectsPage />, m.fetch);
    const form = await openPublish();
    const checks = within(form).getByLabelText("Checks");
    fireEvent.change(checks, { target: { value: 'smoke=python3 -c "print(123)"' } });
    const argv = within(form).getByRole("list", { name: "Arguments of smoke" });
    expect(within(argv).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["python3", "-c", "print(123)"]);
    fireEvent.change(checks, { target: { value: 'smoke=python3 -c "print(123)' } });
    expect(within(form).getByRole("alert")).toHaveTextContent("Line 1: the quote opened at column 18 is not closed");
    expect(within(form).getByRole("button", { name: "Publish version" })).toBeDisabled();
    fireEvent.change(checks, { target: { value: "chain=pytest -q && echo ok" } });
    expect(within(form).getByRole("note")).toHaveTextContent("&& is passed to the program as a plain argument");
  });

  it("asks for confirmation before a version changes the delivery policy", async () => {
    let sent: { spec: ProjectSpec } | null = null;
    const following = { ...FULL, ship_policy: { ...FULL.ship_policy, base_branch: "develop" } };
    const m = mockFetch(projectRoutes(project(following), (b) => (sent = b as typeof sent)));
    await renderApp(<ProjectsPage />, m.fetch);
    const form = await openPublish();
    const ref = within(form).getByLabelText("Base ref");
    await userEvent.clear(ref);
    await userEvent.type(ref, "main");
    const publish = within(form).getByRole("button", { name: "Publish version" });
    expect(publish).toBeDisabled();
    await userEvent.click(within(form).getByRole("checkbox", { name: /changes the delivery policy/ }));
    await userEvent.click(publish);
    await waitFor(() => expect(sent).not.toBeNull());
    expect(sent!.spec.ship_policy?.base_branch).toBe("main");
    expect(sent!.spec.ship_policy?.required_results).toEqual(FULL.ship_policy!.required_results);
  });

  it("declares a service with an argv preview (AUD17 configuration path)", async () => {
    let sent: { spec: ProjectSpec } | null = null;
    const m = mockFetch(projectRoutes(project({ ...FULL, services: [] }), (b) => (sent = b as typeof sent)));
    await renderApp(<ProjectsPage />, m.fetch);
    const form = await openPublish();
    await userEvent.click(within(form).getByRole("button", { name: "Add service" }));
    const row = within(form).getByRole("group", { name: "Service …" });
    await userEvent.type(within(row).getByLabelText("Name"), "api");
    fireEvent.change(within(form).getByLabelText("Command"), { target: { value: "python3 -m http.server 8000 --directory 'public html'" } });
    await userEvent.type(within(form).getByLabelText("Port"), "8000");
    expect(within(form).getByRole("list", { name: "Arguments of api" })).toHaveTextContent("public html");
    await userEvent.click(within(form).getByRole("button", { name: "Publish version" }));
    await waitFor(() => expect(sent).not.toBeNull());
    expect(sent!.spec.services).toEqual([{ name: "api", argv: ["python3", "-m", "http.server", "8000", "--directory", "public html"], port: 8000 }]);
  });
});

describe("Services tab (AUD17)", () => {
  it("explains where services come from when a projectless Session has none", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/services", { json: { items: [], source: { kind: "none" }, capabilities: { preview: { enabled: false } } } }],
    ]);
    await renderApp(<ServicesTab sessionId="s1" />, m.fetch);
    expect(await screen.findByText("This Session has no Project, so it has no declared services.")).toBeInTheDocument();
    expect(screen.getByText(/Processes the agent or a terminal starts on their own are not listed/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Projects" })).toHaveAttribute("href", "/projects");
  });

  it("names the declaring version, says preview is unavailable and that logs are not started yet", async () => {
    const m = mockFetch([
      ...identityRoutes,
      [
        "GET",
        "/api/sessions/s1/services",
        {
          json: {
            items: [{ name: "web", desired: "stopped", state: "stopped", port: 8000, preview: true, preview_available: false, lease_live: true }],
            source: { kind: "project_version", project_id: "p1", project_name: "Shop", project_version_id: "pv1", project_version_ordinal: 2, current_version_id: "pv2" },
            capabilities: { preview: { enabled: false, reason: "no preview origin" } },
          },
        },
      ],
      ["GET", "/api/sessions/s1/services/web/logs", { json: { name: "web", lines: [], started: false } }],
    ]);
    await renderApp(<ServicesTab sessionId="s1" />, m.fetch);
    expect(await screen.findByText(/Declared by Project Shop, version 2\./)).toHaveTextContent("A newer version exists");
    expect(screen.getByText(/Preview links are not available on this deployment/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open preview" })).toBeNull();
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Logs" }));
    expect(await screen.findByText("Not started on the current compute yet.")).toBeInTheDocument();
  });
});

const ready: ChangeSet = {
  id: "cs1",
  session_id: "s1",
  source_turn_id: null,
  state: "ready",
  origin: "explicit",
  subject_digest: "sha256:subject",
  repository: null,
  base_sha: null,
  head_sha: null,
  worktree_generation: 1,
  file_count: 1,
  error: null,
  created_at: "2026-10-15T00:00:00Z",
  sealed_at: "2026-10-15T00:00:01Z",
};

const capability = (over: Partial<DeliveryCapability>): DeliveryCapability => ({
  available: false,
  reason: null,
  message: null,
  transport: "pull_request",
  repository: null,
  base_branch: null,
  draft: true,
  connection_id: null,
  recovery: [],
  patch_available: true,
  ...over,
});

function changesRoutes(delivery: DeliveryCapability) {
  const cs = { ...ready, delivery };
  return [
    ...identityRoutes,
    ["GET", "/api/sessions/s1/changes", { json: { live: true, observation: { files: [] } } }],
    ["GET", "/api/sessions/s1/changesets", { json: { items: [cs] } }],
    ["GET", "/api/changesets/cs1", { json: { ...cs, files: [{ path: "notes.txt", type: "added" }] } }],
    ["GET", "/api/changesets/cs1/deliveries", { json: { items: [] } }],
  ] as Parameters<typeof mockFetch>[0];
}

describe("Delivery capability in Changes (AUD15)", () => {
  it("offers the patch, not a pull request form, for a Session without a repository", async () => {
    const m = mockFetch(changesRoutes(capability({ reason: "no_repository", recovery: ["download_patch"] })));
    await renderApp(<ChangesTab sessionId="s1" />, m.fetch);
    expect(await screen.findByText("This Session has no repository")).toBeInTheDocument();
    expect(screen.queryByLabelText("Pull request title")).toBeNull();
    expect(screen.queryByRole("button", { name: "Request delivery" })).toBeNull();
    const link = screen.getByRole("link", { name: "Download patch" });
    expect(link).toHaveAttribute("href", "/api/changesets/cs1/patch");
    expect(link).toHaveAttribute("download", "cs1.patch");
    expect(m.calls.filter((c) => c.method !== "GET")).toHaveLength(0);
  });

  it("points to Connections when GitHub is missing", async () => {
    const m = mockFetch(changesRoutes(capability({ reason: "connection_required", repository: "acme/demo", recovery: ["manage_connections"] })));
    await renderApp(<ChangesTab sessionId="s1" />, m.fetch);
    expect(await screen.findByText("GitHub is not connected")).toBeInTheDocument();
    expect(screen.getByText(/Delivering to acme\/demo needs a GitHub Connection/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Connections" })).toHaveAttribute("href", "/connections");
    expect(screen.queryByRole("link", { name: "Download patch" })).toBeNull();
  });

  it("shows the target and the policy's draft default when delivery is available", async () => {
    const m = mockFetch([
      ...changesRoutes(capability({ available: true, repository: "acme/demo", base_branch: "main", draft: false, connection_id: "c1" })),
      ["POST", "/api/changesets/cs1/deliveries", { status: 202, json: { delivery: { id: "d1" } } }],
    ]);
    await renderApp(<ChangesTab sessionId="s1" />, m.fetch);
    expect(await screen.findByText("Opens a pull request on acme/demo against main.")).toBeInTheDocument();
    expect(screen.getByLabelText("Draft")).not.toBeChecked();
    await userEvent.click(screen.getByRole("button", { name: "Request delivery" }));
    await waitFor(() => expect(m.find("POST", "/api/changesets/cs1/deliveries")).toHaveLength(1));
    expect(m.find("POST", "/api/changesets/cs1/deliveries")[0].body).toEqual({ draft: false });
  });
});

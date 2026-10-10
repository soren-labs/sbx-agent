import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Harness, MachineSlot, MachineSlotList, ModelsView, SlotCatalog } from "../api/types";
import { MachinesPage } from "../features/machines/MachinesPage";
import { NewSession } from "../features/sessions/NewSession";
import { connection, identityRoutes, mockFetch, renderApp, session } from "../test/helpers";

const PROVIDERS: MachineSlotList["providers"] = [
  {
    provider_id: "codex",
    display_name: "Codex",
    harness_provider_id: "codex",
    login_method: "device_code",
    verification_host: "auth.openai.com",
    login_window_seconds: 900,
    catalog_source: "codex app-server model/list",
    available: true,
  },
];
/** Shaped like the CLI's answer; the ids are placeholders, not product data. */
const CATALOG: SlotCatalog = {
  status: "ready",
  source: "codex app-server model/list",
  observed_at: "2026-10-10T09:00:00+00:00",
  cli_version: "codex-cli 0.162.0",
  default_model: "model-a",
  models: [
    {
      id: "model-a",
      name: "Model A",
      description: null,
      default: true,
      reasoning: {
        kind: "levels",
        default: "medium",
        efforts: [
          { id: "low", description: "Fast" },
          { id: "medium", description: "Balanced" },
          { id: "ultra", description: "Maximum" },
        ],
      },
    },
    { id: "model-b", name: "Model B", description: null, default: false, reasoning: { kind: "levels", default: "low", efforts: [{ id: "low", description: "Fast" }] } },
  ],
};

function slot(over: Partial<MachineSlot> & { id: string }): MachineSlot {
  return {
    workspace_id: "w1",
    provider: "codex",
    provider_name: "Codex",
    label: over.id,
    account_alias: null,
    state: "ready",
    status: "ready",
    state_reason: null,
    busy: false,
    worker: null,
    compute_connection_id: "c_modal",
    volume: { name: `sbx-slot-${over.id}`, filesystem: "modal_volume_v2", managed: true },
    login: null,
    capabilities: { catalog: CATALOG, cli_version: "codex-cli 0.162.0" },
    verified_at: "2026-10-10T09:00:00+00:00",
    last_used_at: null,
    version: 1,
    created_at: "2026-10-10T08:00:00+00:00",
    updated_at: "2026-10-10T09:00:00+00:00",
    ...over,
  };
}

function listing(items: MachineSlot[]): MachineSlotList {
  const count = (s: string[]) => items.filter((i) => s.includes(i.status)).length;
  return {
    items,
    providers: PROVIDERS,
    summary: {
      total: items.length,
      running: count(["running"]),
      ready: count(["ready"]),
      login_pending: count(["login_pending"]),
      needs_attention: count(["needs_login", "error"]),
    },
  };
}

const EIGHTEEN = Array.from({ length: 18 }, (_, i) =>
  slot({
    id: `slot_${i + 1}`,
    label: `Machine ${i + 1}`,
    account_alias: i % 2 ? "team account" : null,
    ...(i === 3
      ? { status: "running", busy: true, worker: { lease_id: "lease_1", session_id: "s9" } }
      : i === 7
        ? { status: "needs_login", state: "needs_login", state_reason: "code_expired", capabilities: {} }
        : {}),
  }),
);

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("My Cloud Machines", () => {
  it("fits 18 machines as compact collapsed rows with counts, search, filter and a remembered group state", async () => {
    const user = userEvent.setup();
    const { fetch } = mockFetch([...identityRoutes, ["GET", "/api/workspaces/w1/machine-slots", { json: listing(EIGHTEEN) }]]);
    await renderApp(<MachinesPage />, fetch);

    expect(await screen.findByTestId("machine-summary")).toHaveTextContent("18 machines · 1 running · 16 ready · 1 need attention");
    const rows = screen.getAllByTestId("machine-row");
    expect(rows).toHaveLength(18);
    // Compact by default: every row is collapsed, no login form or detail is rendered.
    expect(rows.every((r) => within(r).getByRole("button").getAttribute("aria-expanded") === "false")).toBe(true);
    expect(screen.queryByText("Volume")).not.toBeInTheDocument();
    expect(within(rows[0]).getByText("Machine 1")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Ready")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Codex.*18 machines · 1 running/ })).toHaveAttribute("aria-expanded", "true");

    await user.type(screen.getByRole("searchbox", { name: "Search machines" }), "machine 12");
    expect(screen.getAllByTestId("machine-row")).toHaveLength(1);
    await user.clear(screen.getByRole("searchbox", { name: "Search machines" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by status" }), "attention");
    expect(screen.getAllByTestId("machine-row").map((r) => r.dataset.status)).toEqual(["needs_login"]);
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by status" }), "running");
    expect(within(screen.getByTestId("machine-row")).getByText("Running")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by status" }), "all");
    await user.selectOptions(screen.getByRole("combobox", { name: "Sort" }), "status");
    expect(screen.getAllByTestId("machine-row")[0].dataset.status).toBe("running");

    await user.click(screen.getByRole("button", { name: /Codex.*18 machines/ }));
    expect(screen.queryAllByTestId("machine-row")).toHaveLength(0);
    expect(JSON.parse(localStorage.getItem("sbx.machines.collapsed")!)).toEqual(["codex"]);
  });

  it("expands one machine for details and explains why another needs a login", async () => {
    const user = userEvent.setup();
    const { fetch } = mockFetch([...identityRoutes, ["GET", "/api/workspaces/w1/machine-slots", { json: listing(EIGHTEEN) }]]);
    await renderApp(<MachinesPage />, fetch);
    const rows = await screen.findAllByTestId("machine-row");
    await user.click(within(rows[0]).getByRole("button", { name: /Machine 1\b/ }));
    expect(within(rows[0]).getByText("2 models, reported by codex app-server model/list")).toBeInTheDocument();
    expect(within(rows[0]).getByText("sbx-slot-slot_1")).toBeInTheDocument();
    const attention = screen.getAllByTestId("machine-row").find((r) => r.dataset.status === "needs_login")!;
    await user.click(within(attention).getByRole("button", { name: /Machine 8/ }));
    expect(within(attention).getByText(/The code expired before it was approved/)).toBeInTheDocument();
    expect(within(attention).getByRole("button", { name: "Log in" })).toBeInTheDocument();
    // One row open at a time keeps the page compact.
    expect(within(screen.getAllByTestId("machine-row")[0]).getByRole("button", { name: /Machine 1\b/ })).toHaveAttribute("aria-expanded", "false");
    const busy = screen.getAllByTestId("machine-row").find((r) => r.dataset.status === "running")!;
    await user.click(within(busy).getByRole("button", { name: /Machine 4/ }));
    expect(within(busy).getByRole("link", { name: "Open the Session" })).toHaveAttribute("href", "/sessions/s9");
    expect(within(busy).queryByRole("button", { name: "Delete machine" })).not.toBeInTheDocument();
  });

  it("adds a machine, shows the real URL and code, and turns Ready by itself with no save step", async () => {
    const user = userEvent.setup();
    const login = (state: string, extra: object = {}) => ({
      attempt_id: "a1",
      mode: "login",
      state,
      verification_url: null,
      user_code: null,
      code_expires_at: null,
      error_code: null,
      started_at: "2026-10-10T09:00:00+00:00",
      finished_at: null,
      ...extra,
    });
    const pending = (l: object) => slot({ id: "slot_new", label: "Codex 1", state: "login_pending", status: "login_pending", capabilities: {}, verified_at: null, login: l as MachineSlot["login"] });
    const phases: MachineSlotList[] = [
      listing([]),
      listing([pending(login("starting"))]),
      listing([pending(login("awaiting_user", { verification_url: "https://auth.openai.com/codex/device", user_code: "ABCD-12345", code_expires_at: new Date(Date.now() + 600_000).toISOString() }))]),
      listing([pending(login("verifying", { verification_url: "https://auth.openai.com/codex/device" }))]),
      listing([slot({ id: "slot_new", label: "Codex 1", login: login("succeeded", { finished_at: "2026-10-10T09:05:00+00:00" }) as MachineSlot["login"] })]),
    ];
    let phase = 0;
    const { fetch, find } = mockFetch([
      ...identityRoutes,
      ["GET", "/api/workspaces/w1/machine-slots", () => ({ json: phases[Math.min(phase, phases.length - 1)] })],
      ["POST", "/api/workspaces/w1/machine-slots", () => ({ status: 201, json: phases[1].items[0] })],
    ]);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await renderApp(<MachinesPage />, fetch);

    expect(await screen.findByText("No machines yet")).toBeInTheDocument();
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    phase = 1;
    await user.click(screen.getByRole("button", { name: "Add Codex machine" }));
    expect(find("POST", "/api/workspaces/w1/machine-slots")[0].body).toEqual({ provider: "codex" });
    expect(await screen.findByText(/Starting a setup machine in your Modal workspace/)).toBeInTheDocument();
    // With many machines the new row opens below the fold, so it is brought into view.
    expect(scrolled).toHaveBeenCalled();

    phase = 2;
    const code = await screen.findByTestId("device-code", undefined, { timeout: 5000 });
    expect(code).toHaveTextContent("ABCD-12345");
    const open = screen.getByRole("link", { name: "Open sign-in page" });
    expect(open).toHaveAttribute("href", "https://auth.openai.com/codex/device");
    expect(open).toHaveAttribute("target", "_blank");
    expect(open.getAttribute("href")).not.toContain("ABCD-12345"); // no invented deep link
    await user.click(screen.getByRole("button", { name: "Copy code" }));
    expect(writeText).toHaveBeenCalledWith("ABCD-12345");
    expect(screen.getByText(/code expires in \d+:\d\d/)).toBeInTheDocument();

    phase = 3;
    expect(await screen.findByText(/Approved\. Verifying with a real request/, undefined, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByTestId("device-code")).not.toBeInTheDocument();

    phase = 4;
    await waitFor(() => expect(screen.getByTestId("machine-row").dataset.status).toBe("ready"), { timeout: 5000 });
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    expect(screen.queryByText("ABCD-12345")).not.toBeInTheDocument();
    expect(screen.getByText("2 models, reported by codex app-server model/list")).toBeInTheDocument();
  }, 20000);

  it("cancels a login and deletes only after the name is typed", async () => {
    const user = userEvent.setup();
    const waiting = slot({
      id: "slot_w",
      label: "Waiting",
      state: "login_pending",
      status: "login_pending",
      capabilities: {},
      login: { attempt_id: "a", mode: "login", state: "awaiting_user", verification_url: "https://auth.openai.com/codex/device", user_code: "WXYZ-99999", code_expires_at: null, error_code: null, started_at: "", finished_at: null },
    });
    const { fetch, find } = mockFetch([
      ...identityRoutes,
      ["GET", "/api/workspaces/w1/machine-slots", { json: listing([waiting, slot({ id: "slot_d", label: "Old box" })]) }],
      ["DELETE", "/api/machine-slots/slot_w/logins/current", { status: 202, json: waiting }],
      ["DELETE", "/api/machine-slots/slot_d", { status: 202, json: slot({ id: "slot_d", label: "Old box", status: "deleting" }) }],
    ]);
    await renderApp(<MachinesPage />, fetch);
    // A login already in progress is opened without hunting for it.
    expect(await screen.findByTestId("device-code")).toHaveTextContent("WXYZ-99999");
    await user.click(screen.getByRole("button", { name: "Cancel login" }));
    await waitFor(() => expect(find("DELETE", "/api/machine-slots/slot_w/logins/current")).toHaveLength(1));

    const row = screen.getAllByTestId("machine-row").find((r) => within(r).queryByText("Old box"))!;
    await user.click(within(row).getByRole("button", { name: /Old box/ }));
    await user.click(within(row).getByRole("button", { name: "Delete machine" }));
    const confirm = within(row).getByRole("button", { name: "Delete machine" });
    expect(confirm).toBeDisabled();
    await user.type(within(row).getByRole("textbox", { name: /Type “Old box” to confirm/ }), "Old box");
    await user.click(within(row).getByRole("button", { name: "Delete machine" }));
    await waitFor(() => expect(find("DELETE", "/api/machine-slots/slot_d")).toHaveLength(1));
    expect(find("DELETE", "/api/machine-slots/slot_d")[0].query.get("confirm")).toBe("Old box");
  });

  it("says what is missing when Modal is not connected", async () => {
    const { fetch } = mockFetch([
      ...identityRoutes,
      ["GET", "/api/workspaces/w1/machine-slots", { json: { ...listing([]), providers: [{ ...PROVIDERS[0], available: false }] } }],
    ]);
    await renderApp(<MachinesPage />, fetch);
    expect(await screen.findByText(/Machines need a verified Modal connection/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add Codex machine" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "Connect Modal" })).toHaveAttribute("href", "/connections");
  });
});

// -- New Session: machine, real catalog, per-model effort ---------------------------------
const HARNESSES: Harness[] = [
  { provider_id: "opencode", support_tier: "supported", inference_protocols: ["openai_chat", "anthropic_messages"], capabilities: {} },
  { provider_id: "codex", support_tier: "supported", inference_protocols: ["openai_responses"], capabilities: {} },
];
const DEEPSEEK = connection({
  kind: "inference_api",
  id: "c_chat",
  label: "DeepSeek",
  config: { endpoints: { openai_chat: "https://api.example.test" }, model: "flash", models: ["flash", "plain"] },
});
function models(provider: string): ModelsView {
  const chat = provider === "opencode";
  return {
    provider_id: provider,
    inference_protocols: chat ? ["openai_chat"] : ["openai_responses"],
    preferred_model: chat ? "flash" : null,
    connections: [
      {
        connection_id: "c_chat",
        label: "DeepSeek",
        health: "ready",
        preferred_model: "flash",
        // "flash" has a verified thinking switch; "plain" has no reasoning control.
        models: [
          { id: "flash", efforts: chat ? ["none"] : [] },
          { id: "plain", efforts: [] },
        ],
        protocols: ["openai_chat"],
        protocol: chat ? "openai_chat" : null,
        compatible: chat,
      },
    ],
  };
}
const OTHER_CATALOG: SlotCatalog = { ...CATALOG, default_model: "model-z", models: [{ id: "model-z", name: "Model Z", description: null, default: true, reasoning: { kind: "none", default: null, efforts: [] } }] };

function composerRoutes(slots: MachineSlot[], withKey = true) {
  return mockFetch([
    ...identityRoutes,
    ["GET", "/api/workspaces/w1/connections", { json: { items: [connection({ kind: "modal", id: "c_modal" }), ...(withKey ? [DEEPSEEK] : [])] } }],
    ["GET", "/api/harnesses", { json: { items: HARNESSES } }],
    ["GET", "/api/executor-backends", { json: { items: [{ kind: "modal" }, { kind: "local" }] } }],
    ["GET", "/api/workspaces/w1/projects", { json: { items: [] } }],
    ["GET", "/api/models", (call) => ({ json: withKey ? models(call.query.get("provider_id") ?? "opencode") : { provider_id: "opencode", preferred_model: null, connections: [] } })],
    ["GET", "/api/workspaces/w1/machine-slots", { json: listing(slots) }],
    ["POST", "/api/workspaces/w1/sessions", { status: 202, json: { session_id: "s1", session: session(), event_watermark: 0 } }],
  ]);
}

describe("New Session on a machine", () => {
  const A = slot({ id: "slot_a", label: "A1" });
  const B = slot({ id: "slot_b", label: "B1", capabilities: { catalog: OTHER_CATALOG } });
  const BUSY = slot({ id: "slot_busy", label: "A2", status: "running", busy: true });
  const OUT = slot({ id: "slot_out", label: "A3", status: "needs_login", state: "needs_login", capabilities: {} });

  it("offers only the selected machine's reported models and that model's own efforts", async () => {
    const user = userEvent.setup();
    const { fetch, find } = composerRoutes([A, B, BUSY, OUT]);
    await renderApp(<NewSession />, fetch);
    await user.click(await screen.findByTestId("agent-model-chip"));
    const sources = within(screen.getByTestId("source-options"));
    expect(sources.getByRole("button", { name: /Custom API key/ })).toHaveAttribute("aria-pressed", "true");
    expect(sources.getByRole("button", { name: /A2.*in use/ })).toBeDisabled();
    // Machines that cannot run are not listed; one link leads to them.
    expect(sources.queryByRole("button", { name: /A3/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "1 more not ready · manage machines" })).toHaveAttribute("href", "/machines");

    await user.click(sources.getByRole("button", { name: /A1/ }));
    expect(screen.getByTestId("model-source")).toHaveTextContent("Models reported by codex app-server model/list");
    const modelList = within(screen.getByTestId("model-options"));
    expect(modelList.getAllByRole("button").map((b) => b.textContent)).toEqual(["Model A", "Model B"]);
    expect(screen.queryByRole("textbox", { name: /custom model/i })).not.toBeInTheDocument(); // no free-typed ids
    const efforts = () => within(screen.getByTestId("effort-options")).getAllByRole("button").map((b) => b.textContent);
    expect(efforts()).toEqual(["Model default", "low", "medium", "ultra"]);
    await user.click(within(screen.getByTestId("effort-options")).getByRole("button", { name: "ultra" }));

    // Another model has its own, shorter list; the previous choice is dropped, not carried over.
    await user.click(modelList.getByRole("button", { name: "Model B" }));
    expect(efforts()).toEqual(["Model default", "low"]);
    await user.click(within(screen.getByTestId("effort-options")).getByRole("button", { name: "low" }));

    await user.type(screen.getByRole("textbox", { name: /task/i }), "fix it");
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(find("POST", "/api/workspaces/w1/sessions")).toHaveLength(1));
    expect(find("POST", "/api/workspaces/w1/sessions")[0].body).toMatchObject({
      harness: { provider_id: "codex", model: "model-b", effort: "low" },
      inference: { mode: "subscription", machine_slot_id: "slot_a" },
      executor: { backend: "modal" },
    });
    expect((find("POST", "/api/workspaces/w1/sessions")[0].body as { connections?: unknown }).connections).toBeUndefined();
  });

  it("changing the machine changes the model choices, and a model without efforts shows none", async () => {
    const user = userEvent.setup();
    const { fetch } = composerRoutes([A, B]);
    await renderApp(<NewSession />, fetch);
    await user.click(await screen.findByTestId("agent-model-chip"));
    await user.click(within(screen.getByTestId("source-options")).getByRole("button", { name: /A1/ }));
    expect(within(screen.getByTestId("model-options")).getAllByRole("button")).toHaveLength(2);
    await user.click(within(screen.getByTestId("source-options")).getByRole("button", { name: /B1/ }));
    expect(within(screen.getByTestId("model-options")).getAllByRole("button").map((b) => b.textContent)).toEqual(["Model Z"]);
    expect(screen.queryByTestId("effort-options")).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /Reasoning effort/ })).toBeDisabled();
  });

  it("gives a custom API model a thinking switch only where it was verified", async () => {
    const user = userEvent.setup();
    const { fetch, find } = composerRoutes([A]);
    await renderApp(<NewSession />, fetch);
    await user.click(await screen.findByTestId("agent-model-chip"));
    await waitFor(() => expect(within(screen.getByTestId("model-options")).getAllByRole("button").length).toBeGreaterThan(0));
    // DeepSeek-style binary thinking: On / Off, never low / medium / high.
    expect(within(screen.getByTestId("effort-options")).getAllByRole("button").map((b) => b.textContent)).toEqual(["On (model default)", "Off"]);
    await user.click(within(screen.getByTestId("model-options")).getByRole("button", { name: "plain" }));
    expect(screen.queryByTestId("effort-options")).not.toBeInTheDocument();
    await user.click(within(screen.getByTestId("model-options")).getByRole("button", { name: "flash" }));
    await user.click(within(screen.getByTestId("effort-options")).getByRole("button", { name: "Off" }));
    await user.type(screen.getByRole("textbox", { name: /task/i }), "fix it");
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(find("POST", "/api/workspaces/w1/sessions")).toHaveLength(1));
    const body = find("POST", "/api/workspaces/w1/sessions")[0].body as Record<string, unknown>;
    expect(body.harness).toEqual({ provider_id: "opencode", model: "flash", effort: "none" });
    expect(body.inference).toBeUndefined();
  });

  it("preselects a ready machine when there is no API key, and never guesses models without a catalog", async () => {
    const user = userEvent.setup();
    const bare = slot({ id: "slot_bare", label: "Bare", capabilities: { catalog: { ...CATALOG, status: "unavailable", models: [], default_model: null } } });
    const { fetch, find } = composerRoutes([bare], false);
    await renderApp(<NewSession />, fetch);
    const chip = await screen.findByTestId("agent-model-chip");
    await waitFor(() => expect(chip).toHaveTextContent("Bare"));
    await user.click(chip);
    expect(screen.getByTestId("model-source")).toHaveTextContent(/no verified model list/);
    expect(within(screen.getByTestId("model-options")).queryAllByRole("button")).toHaveLength(0);
    await user.type(screen.getByRole("textbox", { name: /task/i }), "go");
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(find("POST", "/api/workspaces/w1/sessions")).toHaveLength(1));
    expect(find("POST", "/api/workspaces/w1/sessions")[0].body).toMatchObject({
      harness: { provider_id: "codex" },
      inference: { mode: "subscription", machine_slot_id: "slot_bare" },
    });
  });
});

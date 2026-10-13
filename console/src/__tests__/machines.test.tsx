import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Harness, MachineSlot, MachineSlotList, ModelsView, SlotCatalog } from "../api/types";
import { MachinesPage } from "../features/machines/MachinesPage";
import { NewSessionComposer } from "../features/composer/Composer";
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

const THREE_PROVIDERS: MachineSlotList["providers"] = [
  ...PROVIDERS,
  {
    provider_id: "antigravity",
    display_name: "Antigravity",
    harness_provider_id: "antigravity",
    login_method: "authorization_code",
    verification_host: "accounts.google.com",
    login_window_seconds: 900,
    catalog_source: "agy models",
    available: true,
  },
  {
    provider_id: "grok",
    display_name: "Grok Build",
    harness_provider_id: "grok",
    login_method: "device_code",
    verification_host: "accounts.x.ai",
    login_window_seconds: 900,
    catalog_source: "grok agent stdio initialize",
    available: true,
  },
];

function listing(items: MachineSlot[], providers = PROVIDERS): MachineSlotList {
  const count = (s: string[]) => items.filter((i) => s.includes(i.status)).length;
  return {
    items,
    providers,
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
    // Compact by default: every row head is collapsed, no detail is rendered.
    expect(rows.every((r) => within(r).getAllByRole("button")[0].getAttribute("aria-expanded") === "false")).toBe(true);
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
    expect(within(screen.getByTestId("machine-row")).getByText("In use")).toBeInTheDocument();
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
    expect(within(rows[0]).getAllByText("2 models, reported by codex app-server model/list").length).toBeGreaterThan(0);
    expect(within(rows[0]).getByText("sbx-slot-slot_1")).toBeInTheDocument();
    const attention = screen.getAllByTestId("machine-row").find((r) => r.dataset.status === "needs_login")!;
    await user.click(within(attention).getByRole("button", { name: /Machine 8/ }));
    expect(within(attention).getByText(/The code expired before it was approved/)).toBeInTheDocument();
    expect(within(attention).getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    // One row open at a time keeps the page compact.
    expect(within(screen.getAllByTestId("machine-row")[0]).getByRole("button", { name: /Machine 1\b/ })).toHaveAttribute("aria-expanded", "false");
    const busy = screen.getAllByTestId("machine-row").find((r) => r.dataset.status === "running")!;
    await user.click(within(busy).getByRole("button", { name: /Machine 4/ }));
    expect(within(busy).getByRole("link", { name: "Open the Session" })).toHaveAttribute("href", "/sessions/s9");
    // A busy machine cannot be removed: the menu does not offer it.
    await user.click(within(busy).getByRole("button", { name: "Machine actions" }));
    expect(screen.queryByRole("menuitem", { name: "Delete machine" })).not.toBeInTheDocument();
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
    // "Add Codex machine" opens a dialog; submitting it creates the slot.
    await user.click(screen.getByRole("button", { name: "Add Codex machine" }));
    const dialog = await screen.findByRole("dialog", { name: "Add machine" });
    await user.click(within(dialog).getByRole("button", { name: "Add machine" }));
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
    expect(screen.getAllByText(/2 models, reported by codex app-server model\/list/).length).toBeGreaterThan(0);
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
    // A login already in progress is opened in a dialog without hunting for it.
    expect(await screen.findByTestId("device-code")).toHaveTextContent("WXYZ-99999");
    await user.click(screen.getByRole("button", { name: "Cancel login" }));
    await waitFor(() => expect(find("DELETE", "/api/machine-slots/slot_w/logins/current")).toHaveLength(1));

    const row = screen.getAllByTestId("machine-row").find((r) => within(r).queryByText("Old box"))!;
    await user.click(within(row).getByRole("button", { name: "Machine actions" }));
    await user.click(screen.getByRole("menuitem", { name: "Delete machine" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove machine" });
    const confirm = within(dialog).getByRole("button", { name: "Delete machine" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByRole("textbox", { name: /Type “Old box” to confirm/ }), "Old box");
    await user.click(within(dialog).getByRole("button", { name: "Delete machine" }));
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

function ComposerHost() {
  const [prompt, setPrompt] = useState("");
  return <NewSessionComposer prompt={prompt} onPromptChange={setPrompt} />;
}

function renderComposer(fetch: typeof globalThis.fetch) {
  return renderApp(
    <Routes>
      <Route path="/" element={<ComposerHost />} />
      <Route path="/sessions/:id" element={<div>opened session</div>} />
      <Route path="/machines" element={<div>machines page</div>} />
    </Routes>,
    fetch,
  );
}

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
    await renderComposer(fetch);
    await user.click(await screen.findByTestId("agent-model-chip"));
    const sources = within(screen.getByTestId("source-options"));
    expect(sources.getByRole("button", { name: /Custom API key|API key/ })).toHaveAttribute("aria-pressed", "true");
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
    await renderComposer(fetch);
    await user.click(await screen.findByTestId("agent-model-chip"));
    await user.click(within(screen.getByTestId("source-options")).getByRole("button", { name: /A1/ }));
    expect(within(screen.getByTestId("model-options")).getAllByRole("button")).toHaveLength(2);
    await user.click(within(screen.getByTestId("source-options")).getByRole("button", { name: /B1/ }));
    expect(within(screen.getByTestId("model-options")).getAllByRole("button").map((b) => b.textContent)).toEqual(["Model Z"]);
    expect(screen.queryByTestId("effort-options")).not.toBeInTheDocument();
  });

  it("gives a custom API model a thinking switch only where it was verified", async () => {
    const user = userEvent.setup();
    const { fetch, find } = composerRoutes([A]);
    await renderComposer(fetch);
    await user.click(await screen.findByTestId("agent-model-chip"));
    await waitFor(() => expect(within(screen.getByTestId("model-options")).getAllByRole("button").length).toBeGreaterThan(0));
    // DeepSeek-style binary thinking: on / off, never low / medium / high.
    expect(within(screen.getByTestId("effort-options")).getAllByRole("button").map((b) => b.textContent)).toEqual(["Thinking on", "Thinking off"]);
    await user.click(within(screen.getByTestId("model-options")).getByRole("button", { name: "plain" }));
    expect(screen.queryByTestId("effort-options")).not.toBeInTheDocument();
    await user.click(within(screen.getByTestId("model-options")).getByRole("button", { name: "flash" }));
    await user.click(within(screen.getByTestId("effort-options")).getByRole("button", { name: "Thinking off" }));
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
    await renderComposer(fetch);
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

/** Placeholder ids shaped like each CLI's answer. */
const AGY_CATALOG: SlotCatalog = {
  status: "ready",
  source: "agy models",
  observed_at: "2026-10-11T09:00:00+00:00",
  cli_version: "1.3.3",
  default_model: null,
  // The CLI lists one slug per effort variant; there is no separate effort.
  models: [
    ["agy-pro-1-high", "Agy Pro 1 (High)"],
    ["agy-pro-1-low", "Agy Pro 1 (Low)"],
  ].map(([id, name]) => ({ id, name, description: null, default: false, reasoning: { kind: "none" as const, default: null, efforts: [] } })),
};
const GROK_CATALOG: SlotCatalog = {
  status: "ready",
  source: "grok agent stdio initialize",
  observed_at: "2026-10-11T09:00:00+00:00",
  cli_version: "grok 1.0.50",
  default_model: "grok-a",
  models: [
    { id: "grok-a", name: "Grok A", description: null, default: true, reasoning: { kind: "levels", default: "high", efforts: [{ id: "xhigh", description: "Extra High" }, { id: "high", description: "High" }] } },
    { id: "grok-b", name: "Grok B", description: null, default: false, reasoning: { kind: "none", default: null, efforts: [] } },
  ],
};
const GOOGLE_URL = `https://accounts.google.com/o/oauth2/auth?client_id=x&state=y&scope=${"s".repeat(400)}`;
const PASTED = "REDACTED-authorization-code";

describe("Three providers", () => {
  const login = (state: string, extra: object = {}) =>
    ({
      attempt_id: "a1",
      mode: "login",
      state,
      method: "authorization_code",
      verification_url: null,
      user_code: null,
      input: null,
      code_expires_at: null,
      error_code: null,
      started_at: "2026-10-11T09:00:00+00:00",
      finished_at: null,
      ...extra,
    }) as MachineSlot["login"];
  const agy = (over: Partial<MachineSlot>) =>
    slot({ id: "slot_agy", label: "Antigravity 1", provider: "antigravity", provider_name: "Antigravity", capabilities: {}, ...over });

  it("groups machines by provider and lets you pick the provider to add", async () => {
    const user = userEvent.setup();
    const items = [
      slot({ id: "c1", label: "Codex 1" }),
      slot({ id: "g1", label: "Grok Build 1", provider: "grok", provider_name: "Grok Build", capabilities: { catalog: GROK_CATALOG } }),
      slot({ id: "g2", label: "Grok Build 2", provider: "grok", provider_name: "Grok Build", status: "needs_login", state: "needs_login", state_reason: "code_rejected", capabilities: {} }),
    ];
    const { fetch, find } = mockFetch([
      ...identityRoutes,
      ["GET", "/api/workspaces/w1/machine-slots", { json: listing(items, THREE_PROVIDERS) }],
      ["POST", "/api/workspaces/w1/machine-slots", { status: 201, json: agy({ status: "login_pending", state: "login_pending", login: login("starting") }) }],
    ]);
    await renderApp(<MachinesPage />, fetch);

    expect(await screen.findByRole("button", { name: /Codex.*1 machine/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Grok Build.*2 machines/ })).toBeInTheDocument();
    // A provider without a machine has no empty group.
    expect(screen.queryByRole("region", { name: "Antigravity" })).not.toBeInTheDocument();

    // A filter that matches nothing in a group says so instead of hiding the group.
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by status" }), "attention");
    expect(screen.getAllByTestId("machine-row")).toHaveLength(1);
    expect(within(screen.getByRole("region", { name: "Codex" })).getByText(/No machine matches/)).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Filter by status" }), "all");

    // "Add machine" opens a dialog whose provider select names how each signs in.
    await user.click(screen.getByRole("button", { name: "Add machine" }));
    const dialog = await screen.findByRole("dialog", { name: "Add machine" });
    const select = within(dialog).getByRole("combobox", { name: "Provider" });
    const options = within(select).getAllByRole("option").map((o) => o.textContent);
    expect(options.map((o) => o?.split(" — ")[0])).toEqual(["Codex", "Antigravity", "Grok Build"]);
    expect(options[1]).toContain("Sign in at accounts.google.com, then paste the code it shows");
    expect(options[2]).toContain("Sign in at accounts.x.ai and confirm a one-time code");

    await user.selectOptions(select, "antigravity");
    await user.click(within(dialog).getByRole("button", { name: "Add machine" }));
    expect(find("POST", "/api/workspaces/w1/machine-slots")[0].body).toEqual({ provider: "antigravity" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("signs in to Antigravity by pasting the code the browser shows, and never keeps it on the page", async () => {
    const user = userEvent.setup();
    const pending = (l: MachineSlot["login"]) => agy({ status: "login_pending", state: "login_pending", verified_at: null, login: l });
    const phases = [
      listing([pending(login("awaiting_user", { verification_url: GOOGLE_URL, input: "authorization_code", code_expires_at: new Date(Date.now() + 600_000).toISOString() }))], THREE_PROVIDERS),
      listing([pending(login("verifying"))], THREE_PROVIDERS),
      listing([agy({ capabilities: { catalog: AGY_CATALOG, cli_version: "1.3.3" }, login: login("succeeded", { finished_at: "2026-10-11T09:05:00+00:00" }) })], THREE_PROVIDERS),
    ];
    let phase = 0;
    let reject = true;
    const { fetch, find } = mockFetch([
      ...identityRoutes,
      ["GET", "/api/workspaces/w1/machine-slots", () => ({ json: phases[phase] })],
      [
        "POST",
        "/api/machine-slots/slot_agy/logins/current/code",
        () =>
          reject
            ? { status: 422, json: { error: { code: "validation_failed", category: "validation", message: "paste the code shown in the browser after signing in", retryable: false, details: { field: "code" }, request_id: "r1" } } }
            : { status: 202, json: phases[1].items[0] },
      ],
    ]);
    await renderApp(<MachinesPage />, fetch);

    const open = await screen.findByRole("link", { name: "Open sign-in page" });
    expect(open).toHaveAttribute("href", GOOGLE_URL);
    // The long sign-in link is opened, not printed; only where it goes is shown.
    expect(screen.getByText("https://accounts.google.com/o/oauth2/auth")).toBeInTheDocument();
    expect(screen.queryByText(GOOGLE_URL)).not.toBeInTheDocument();
    expect(screen.queryByTestId("device-code")).not.toBeInTheDocument();
    expect(screen.getByText("Waiting for the code from the sign-in page.")).toBeInTheDocument();

    const input = screen.getByRole("textbox", { name: "Code shown after sign-in" });
    const send = screen.getByRole("button", { name: "Send code" });
    expect(send).toBeDisabled();
    await user.type(input, "short");
    await user.click(send);
    expect(await screen.findByText("paste the code shown in the browser after signing in")).toBeInTheDocument();
    expect(input).toHaveValue("short"); // a refused code stays so it can be corrected

    reject = false;
    phase = 1;
    await user.clear(input);
    await user.type(input, `  ${PASTED} `);
    await user.click(send);
    await waitFor(() => expect(find("POST", "/api/machine-slots/slot_agy/logins/current/code")).toHaveLength(2));
    expect(find("POST", "/api/machine-slots/slot_agy/logins/current/code")[1].body).toEqual({ code: PASTED });
    // The field goes away at once, so the same code cannot be sent twice.
    await waitFor(() => expect(screen.queryByTestId("login-code-input")).not.toBeInTheDocument());
    expect(await screen.findByText(/Code sent\. Verifying with a real request/, undefined, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByTestId("login-code-input")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(PASTED);

    phase = 2;
    await waitFor(() => expect(screen.getByTestId("machine-row").dataset.status).toBe("ready"), { timeout: 5000 });
    expect(screen.getAllByText(/2 models, reported by agy models/).length).toBeGreaterThan(0);
  }, 20000);

  it("explains a code the provider did not accept", async () => {
    const user = userEvent.setup();
    const rejected = agy({ status: "needs_login", state: "needs_login", state_reason: "code_rejected", login: login("failed", { error_code: "code_rejected" }) });
    const { fetch } = mockFetch([...identityRoutes, ["GET", "/api/workspaces/w1/machine-slots", { json: listing([rejected], THREE_PROVIDERS) }]]);
    await renderApp(<MachinesPage />, fetch);
    const row = await screen.findByTestId("machine-row");
    await user.click(within(row).getByRole("button", { name: /Antigravity 1/ }));
    expect(screen.getByText("The provider did not accept that code. Log in again to get a new sign-in link.")).toBeInTheDocument();
  });

  it("shows the paste-code sign-in in Chinese", async () => {
    localStorage.setItem("sbx.console.locale", "zh-CN");
    const waiting = agy({ status: "login_pending", state: "login_pending", login: login("awaiting_user", { verification_url: GOOGLE_URL, input: "authorization_code" }) });
    const { fetch } = mockFetch([...identityRoutes, ["GET", "/api/workspaces/w1/machine-slots", { json: listing([waiting], THREE_PROVIDERS) }]]);
    await renderApp(<MachinesPage />, fetch);
    expect(await screen.findByRole("textbox", { name: "登录后显示的代码" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送代码" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "添加云电脑" })).toBeInTheDocument();
  });
});

describe("A sign-in link that carries the one-time code", () => {
  it("opens the provider's own link but never prints the code outside the masked field", async () => {
    const waiting = slot({
      id: "slot_g",
      label: "Grok Build 1",
      provider: "grok",
      provider_name: "Grok Build",
      status: "login_pending",
      state: "login_pending",
      capabilities: {},
      login: {
        attempt_id: "a1",
        mode: "login",
        state: "awaiting_user",
        method: "device_code",
        verification_url: "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
        user_code: "ABCD-1234",
        input: null,
        code_expires_at: null,
        error_code: null,
        started_at: "2026-10-11T09:00:00+00:00",
        finished_at: null,
      },
    });
    const { fetch } = mockFetch([...identityRoutes, ["GET", "/api/workspaces/w1/machine-slots", { json: listing([waiting], THREE_PROVIDERS) }]]);
    await renderApp(<MachinesPage />, fetch);
    const code = await screen.findByTestId("device-code");
    expect(code).toHaveTextContent("ABCD-1234");
    expect(screen.getByRole("link", { name: "Open sign-in page" })).toHaveAttribute("href", "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234");
    expect(screen.getByText("https://accounts.x.ai/oauth2/device")).toBeInTheDocument();
    // The code is on the page exactly once, in the element captures blur.
    const elsewhere = document.body.textContent!.replace(code.textContent!, "");
    expect(elsewhere).not.toContain("ABCD-1234");
  });
});

describe("New Session on a Grok Build or Antigravity machine", () => {
  const GROK = slot({ id: "slot_g", label: "Grok Build 1", provider: "grok", provider_name: "Grok Build", capabilities: { catalog: GROK_CATALOG } });
  const AGY = slot({ id: "slot_y", label: "Antigravity 1", provider: "antigravity", provider_name: "Antigravity", capabilities: { catalog: AGY_CATALOG } });
  const routes = () =>
    mockFetch([
      ...identityRoutes,
      ["GET", "/api/workspaces/w1/connections", { json: { items: [connection({ kind: "modal", id: "c_modal" }), DEEPSEEK] } }],
      ["GET", "/api/harnesses", { json: { items: [...HARNESSES, { provider_id: "grok", support_tier: "supported", inference_protocols: ["openai_chat"], capabilities: {} }, { provider_id: "antigravity", support_tier: "supported", inference_protocols: [], capabilities: {} }] } }],
      ["GET", "/api/executor-backends", { json: { items: [{ kind: "modal" }, { kind: "local" }] } }],
      ["GET", "/api/workspaces/w1/projects", { json: { items: [] } }],
      ["GET", "/api/models", (call) => ({ json: models(call.query.get("provider_id") ?? "opencode") })],
      ["GET", "/api/workspaces/w1/machine-slots", { json: listing([slot({ id: "slot_a", label: "A1" }), GROK, AGY], THREE_PROVIDERS) }],
      ["POST", "/api/workspaces/w1/sessions", { status: 202, json: { session_id: "s1", session: session(), event_watermark: 0 } }],
    ]);
  const efforts = () => within(screen.getByTestId("effort-options")).getAllByRole("button").map((b) => b.textContent);

  it("runs the Grok harness with the chosen model's own effort menu", async () => {
    const user = userEvent.setup();
    const { fetch, find } = routes();
    await renderComposer(fetch);
    await user.click(await screen.findByTestId("agent-model-chip"));
    await user.click(within(screen.getByTestId("source-options")).getByRole("button", { name: /Grok Build 1/ }));
    expect(screen.getByTestId("model-source")).toHaveTextContent("Models reported by grok agent stdio initialize");
    expect(efforts()).toEqual(["Model default", "xhigh", "high"]);
    expect(screen.queryByTestId("effort-scope")).not.toBeInTheDocument();
    await user.click(within(screen.getByTestId("effort-options")).getByRole("button", { name: "xhigh" }));
    await user.click(within(screen.getByTestId("model-options")).getByRole("button", { name: "Grok B" }));
    expect(screen.queryByTestId("effort-options")).not.toBeInTheDocument(); // Grok B lists no efforts
    await user.click(within(screen.getByTestId("model-options")).getByRole("button", { name: "Grok A" }));
    await user.click(within(screen.getByTestId("effort-options")).getByRole("button", { name: "xhigh" }));
    await user.type(screen.getByRole("textbox", { name: /task/i }), "fix it");
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(find("POST", "/api/workspaces/w1/sessions")).toHaveLength(1));
    expect(find("POST", "/api/workspaces/w1/sessions")[0].body).toMatchObject({
      harness: { provider_id: "grok", model: "grok-a", effort: "xhigh" },
      inference: { mode: "subscription", machine_slot_id: "slot_g" },
      executor: { backend: "modal" },
    });
  });

  it("runs the Antigravity harness with a model variant and offers no separate effort", async () => {
    const user = userEvent.setup();
    const { fetch, find } = routes();
    await renderComposer(fetch);
    await user.click(await screen.findByTestId("agent-model-chip"));
    await user.click(within(screen.getByTestId("source-options")).getByRole("button", { name: /Antigravity 1/ }));
    expect(screen.getByTestId("model-source")).toHaveTextContent("Models reported by agy models");
    // The CLI's display names carry the effort; the picker shows them as printed.
    expect(within(screen.getByTestId("model-options")).getAllByRole("button").map((b) => b.textContent)).toEqual(["Agy Pro 1 (High)", "Agy Pro 1 (Low)"]);
    expect(screen.queryByTestId("effort-options")).not.toBeInTheDocument();
    await user.click(within(screen.getByTestId("model-options")).getByRole("button", { name: "Agy Pro 1 (Low)" }));
    expect(screen.queryByTestId("effort-options")).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: /task/i }), "fix it");
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(find("POST", "/api/workspaces/w1/sessions")).toHaveLength(1));
    const body = find("POST", "/api/workspaces/w1/sessions")[0].body as { connections?: unknown; harness: { effort?: string } };
    expect(body).toMatchObject({
      harness: { provider_id: "antigravity", model: "agy-pro-1-low" },
      inference: { mode: "subscription", machine_slot_id: "slot_y" },
    });
    expect(body.harness.effort).toBeUndefined(); // an effort next to a variant is refused by the CLI
    expect(body.connections).toBeUndefined(); // never an API key next to a subscription
  });

  it("does not offer Antigravity as a harness for a custom API key", async () => {
    const user = userEvent.setup();
    const { fetch } = routes();
    await renderComposer(fetch);
    await user.click(await screen.findByTestId("agent-model-chip"));
    const names = within(screen.getByTestId("harness-options")).getAllByRole("button").map((b) => b.textContent);
    expect(names).toEqual(["OpenCode", "Codex", "Grok Build"]);
  });
});

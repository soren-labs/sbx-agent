import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { NewSession } from "../features/sessions/NewSession";
import { knownRepositories, modelOptions, pickBackend, pickDefaultModel, pickHarness } from "../features/sessions/defaults";
import type { Harness, ModelsView } from "../api/types";
import { connection, identityRoutes, mockFetch, renderApp, session } from "../test/helpers";

const HARNESSES: Harness[] = [
  { provider_id: "opencode", support_tier: "supported", inference_protocols: ["openai_chat", "anthropic_messages", "openai_responses"], capabilities: {} },
  { provider_id: "codex", support_tier: "supported", inference_protocols: ["openai_responses"], capabilities: {} },
  { provider_id: "claude", support_tier: "supported", inference_protocols: ["anthropic_messages"], capabilities: {} },
  { provider_id: "grok", support_tier: "supported", inference_protocols: ["openai_chat"], capabilities: {} },
  { provider_id: "commandcode", support_tier: "supported", inference_protocols: ["openai_chat", "anthropic_messages", "openai_responses"], capabilities: {} },
  { provider_id: "devin", support_tier: "disabled", capabilities: {} },
];

const CHAT = connection({
  kind: "inference_api",
  id: "c_chat",
  label: "DeepSeek",
  config: { endpoints: { openai_chat: "https://api.example.test" }, model: "flash", models: ["flash"] },
});
const ANTHROPIC = connection({
  kind: "inference_api",
  id: "c_anth",
  label: "Anthropic-compatible",
  config: { endpoints: { anthropic_messages: "https://api.example.test/anthropic" }, model: "sonnet-like", models: ["sonnet-like"] },
});

/** What GET /api/models answers for one Harness given the two Connections above. */
function modelsFor(provider: string): ModelsView {
  const accepted = HARNESSES.find((h) => h.provider_id === provider)?.inference_protocols ?? [];
  return {
    provider_id: provider,
    inference_protocols: accepted,
    preferred_model: accepted.includes("openai_chat") ? "flash" : accepted.includes("anthropic_messages") ? "sonnet-like" : null,
    connections: [
      {
        connection_id: "c_chat",
        label: "DeepSeek",
        health: "ready",
        preferred_model: "flash",
        models: [{ id: "flash" }, { id: "pro" }],
        protocols: ["openai_chat"],
        protocol: accepted.includes("openai_chat") ? "openai_chat" : null,
        compatible: accepted.includes("openai_chat"),
      },
      {
        connection_id: "c_anth",
        label: "Anthropic-compatible",
        health: "ready",
        preferred_model: "sonnet-like",
        models: [{ id: "sonnet-like" }],
        protocols: ["anthropic_messages"],
        protocol: accepted.includes("anthropic_messages") ? "anthropic_messages" : null,
        compatible: accepted.includes("anthropic_messages"),
      },
    ],
  };
}

describe("defaults", () => {
  it("uses the default model of the first Connection the Harness can drive", () => {
    expect(pickDefaultModel(modelsFor("opencode"))).toBe("flash");
    expect(pickDefaultModel(modelsFor("claude"))).toBe("sonnet-like");
    expect(pickDefaultModel(modelsFor("codex"))).toBeUndefined();
    expect(pickDefaultModel(modelsFor("opencode"), "c_anth")).toBe("sonnet-like");
    expect(pickDefaultModel(undefined)).toBeUndefined();
    expect(modelOptions(modelsFor("opencode"))).toEqual(["flash", "pro", "sonnet-like"]);
    expect(modelOptions(modelsFor("grok"))).toEqual(["flash", "pro"]);
    expect(modelOptions(modelsFor("codex"))).toEqual([]);
  });
  it("picks the first Harness an inference Connection can drive, never a disabled one", () => {
    expect(pickHarness(HARNESSES, [CHAT])).toBe("opencode");
    expect(pickHarness(HARNESSES.slice(1), [ANTHROPIC])).toBe("claude");
    expect(pickHarness(HARNESSES.slice(1), [connection({ kind: "modal" })])).toBe("opencode");
    expect(pickHarness(undefined, undefined)).toBe("opencode");
  });
  it("picks Modal when a Modal connection exists", () => {
    expect(pickBackend([connection({ kind: "modal" })])).toBe("modal");
    expect(pickBackend([connection({ kind: "github" })])).toBe("local");
  });
  it("lists only repositories the server reported as reachable", () => {
    const github = connection({
      kind: "github",
      validation: {
        status: "ready",
        observed_at: "2026-01-01T00:00:00Z",
        details: { repositories: { "acme/app": { visible: true, push: true }, "acme/secret": { visible: false } } },
      },
    });
    expect(knownRepositories([github], ["acme/docs"])).toEqual(["acme/app", "acme/docs"]);
    expect(knownRepositories([], [])).toEqual([]);
  });
});

describe("new Session composer", () => {
  const routes = (connections = [connection({ kind: "modal" }), connection({ kind: "github" }), CHAT, ANTHROPIC]) =>
    mockFetch([
      ...identityRoutes,
      ["GET", "/api/workspaces/w1/connections", { json: { items: connections } }],
      ["GET", "/api/harnesses", { json: { items: HARNESSES } }],
      ["GET", "/api/executor-backends", { json: { items: [{ kind: "local" }, { kind: "modal" }] } }],
      ["GET", "/api/models", (c) => ({ json: modelsFor(c.query.get("provider_id") ?? "opencode") })],
      ["GET", "/api/workspaces/w1/projects", { json: { items: [] } }],
      ["POST", "/api/workspaces/w1/sessions", { status: 202, json: { session_id: "s9", turn_id: "t1", session: session({ id: "s9" }), event_watermark: 3 } }],
    ]);
  const render = (m: ReturnType<typeof routes>) =>
    renderApp(
      <Routes>
        <Route path="/" element={<NewSession />} />
        <Route path="/sessions/:id" element={<div>opened session</div>} />
        <Route path="/connections" element={<div>connections page</div>} />
      </Routes>,
      m.fetch,
    );

  it("defaults to a drivable Harness, its Connection's model and Modal", async () => {
    const m = routes();
    await render(m);
    const optionsToggle = screen.getByText("Session options");
    expect(optionsToggle.closest("details")).not.toHaveAttribute("open");
    await userEvent.click(optionsToggle);
    expect(optionsToggle.closest("details")).toHaveAttribute("open");
    const model = (await screen.findByLabelText("Model")) as HTMLSelectElement;
    await waitFor(() => expect(model.value).toBe("flash"));
    expect((screen.getByLabelText("Compute") as HTMLSelectElement).value).toBe("modal");
    const harness = screen.getByLabelText("Harness") as HTMLSelectElement;
    expect(harness.value).toBe("opencode");
    // Every enabled official CLI is offered by name; disabled ones are not.
    expect(within(harness).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "OpenCode",
      "Codex",
      "Claude Code",
      "Grok Build",
      "Command Code",
    ]);

    await userEvent.type(screen.getByLabelText("Repository"), "acme/app");
    // Clicking outside an open popover dismisses it, like any menu.
    await userEvent.type(screen.getByLabelText("Task"), "Fix the failing check");
    expect(optionsToggle.closest("details")).not.toHaveAttribute("open");
    await userEvent.click(screen.getByLabelText("Task"));
    await userEvent.keyboard("{Control>}{Enter}{/Control}");

    await screen.findByText("opened session");
    const post = m.find("POST", "/api/workspaces/w1/sessions")[0];
    expect(post.body).toEqual({
      title: "Fix the failing check",
      harness: { provider_id: "opencode", model: "flash" },
      executor: { backend: "modal" },
      repository: { full_name: "acme/app", base_ref: "main" },
      message: { content: "Fix the failing check" },
    });
    expect(post.headers["Idempotency-Key"]).toBeTruthy();
  });

  it("switching the Harness re-scopes models to the Connections that CLI can use", async () => {
    const m = routes();
    await render(m);
    await userEvent.click(screen.getByText("Session options"));
    const model = (await screen.findByLabelText("Model")) as HTMLSelectElement;
    await waitFor(() => expect(model.value).toBe("flash"));
    await userEvent.selectOptions(model, "pro");
    expect(model.value).toBe("pro");

    await userEvent.selectOptions(screen.getByLabelText("Harness"), "claude");
    // Claude Code speaks only Anthropic Messages: the chat-only key and its models drop out.
    await waitFor(() => expect(model.value).toBe("sonnet-like"));
    expect(within(model).getAllByRole("option").map((o) => o.textContent)).toEqual(["sonnet-like"]);
    expect(screen.getByTestId("agent-model-chip")).toHaveTextContent("Claude Code · sonnet-like");
    expect(m.find("GET", "/api/models").map((c) => c.query.get("provider_id"))).toEqual(["opencode", "claude"]);

    await userEvent.type(screen.getByLabelText("Task"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Start Session" }));
    await screen.findByText("opened session");
    expect(m.find("POST", "/api/workspaces/w1/sessions")[0].body).toEqual({
      title: "hello",
      harness: { provider_id: "claude", model: "sonnet-like" },
      executor: { backend: "modal" },
      message: { content: "hello" },
    });
  });

  it("pins an explicitly chosen inference Connection and its default model", async () => {
    const m = routes();
    await render(m);
    await userEvent.click(screen.getByText("Session options"));
    const pick = (await screen.findByLabelText("Inference connection")) as HTMLSelectElement;
    expect(pick.value).toBe("");
    await userEvent.selectOptions(pick, "c_anth");
    await waitFor(() => expect((screen.getByLabelText("Model") as HTMLSelectElement).value).toBe("sonnet-like"));
    await userEvent.type(screen.getByLabelText("Task"), "hello");
    await userEvent.click(screen.getByRole("button", { name: "Start Session" }));
    await screen.findByText("opened session");
    expect(m.find("POST", "/api/workspaces/w1/sessions")[0].body).toMatchObject({
      harness: { provider_id: "opencode", model: "sonnet-like" },
      connections: { inference: "c_anth" },
    });
  });

  it("explains which protocol is missing when no key can drive the chosen CLI", async () => {
    const m = routes([connection({ kind: "modal" }), CHAT]);
    await render(m);
    await userEvent.click(screen.getByText("Session options"));
    await userEvent.selectOptions(await screen.findByLabelText("Harness"), "codex");
    const hint = await screen.findByRole("status");
    expect(hint).toHaveTextContent("Codex needs an inference API key that offers OpenAI Responses.");
    expect(within(hint).getByRole("link", { name: "Add one in Connections" })).toHaveAttribute("href", "/connections");
    expect(screen.getByTestId("agent-model-chip")).toHaveTextContent("Codex · No model");
  });

  it("offers only real repositories and never invents models", async () => {
    const m = routes();
    await render(m);
    await screen.findByTestId("agent-model-chip");
    await waitFor(() => expect(screen.getByTestId("agent-model-chip")).toHaveTextContent("OpenCode · flash"));
    const html = document.body.innerHTML;
    for (const fake of ["GPT-6.1", "Claude 3.7", "o3-mini", "soren-labs", "big-pickle", "High Effort"]) {
      expect(html).not.toContain(fake);
    }
  });
});

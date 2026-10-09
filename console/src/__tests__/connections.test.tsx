import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ConnectionForm } from "../features/connections/ConnectionForm";
import { ConnectionsPage } from "../features/connections/ConnectionsPage";
import { connection, identityRoutes, mockFetch, renderApp } from "../test/helpers";

const SECRET = "ghp_SUPERSECRETVALUE";
const HARNESSES = [
  { provider_id: "opencode", support_tier: "supported", inference_protocols: ["openai_chat", "anthropic_messages", "openai_responses"], capabilities: {} },
  { provider_id: "codex", support_tier: "supported", inference_protocols: ["openai_responses"], capabilities: {} },
  { provider_id: "claude", support_tier: "supported", inference_protocols: ["anthropic_messages"], capabilities: {} },
  { provider_id: "grok", support_tier: "supported", inference_protocols: ["openai_chat"], capabilities: {} },
];

describe("connection credential entry", () => {
  it("uses password inputs and clears secrets right after submit", async () => {
    const onSubmit = vi.fn();
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    await renderApp(<ConnectionForm kind="github" mode="create" onSubmit={onSubmit} />, mockFetch(identityRoutes).fetch);
    const input = screen.getByLabelText("GitHub token") as HTMLInputElement;
    expect(input.type).toBe("password");
    await userEvent.type(input, SECRET);
    expect(input.value).toBe(SECRET);
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(onSubmit).toHaveBeenCalledWith({ token: SECRET }, "github");
    expect(input.value).toBe("");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(setItem.mock.calls.flat().join(" ")).not.toContain(SECRET);
    setItem.mockRestore();
  });

  it("keeps the submit disabled until every field is filled (Modal needs id + secret)", async () => {
    await renderApp(<ConnectionForm kind="modal" mode="create" onSubmit={vi.fn()} />, mockFetch(identityRoutes).fetch);
    const submit = screen.getByRole("button", { name: "Connect" });
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Modal token ID"), "ak-1");
    expect(submit).toBeDisabled();
    const secret = screen.getByLabelText("Modal token secret") as HTMLInputElement;
    expect(secret.type).toBe("password");
    await userEvent.type(secret, "as-1");
    expect(submit).toBeEnabled();
  });

  it("page: posts the credential once, clears the field, never stores it, and lists no secret", async () => {
    let items = [connection({ kind: "modal" })];
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/harnesses", { json: { items: HARNESSES } }],
      ["GET", "/api/workspaces/w1/connections", () => ({ json: { items } })],
      [
        "POST",
        "/api/workspaces/w1/connections",
        (call) => {
          items = [...items, connection({ kind: "github", id: "c_gh", label: String((call.body as { label: string }).label) })];
          return { status: 201, json: items[1] };
        },
      ],
    ]);
    await renderApp(<ConnectionsPage />, m.fetch);
    const form = await screen.findByRole("form", { name: "Add GitHub (source) connection" });
    const field = form.querySelector("input[type=password]") as HTMLInputElement;
    fireEvent.change(field, { target: { value: SECRET } });
    fireEvent.click(form.querySelector("button[type=submit]")!);
    expect(field.value).toBe("");
    await waitFor(() => expect(m.find("POST", "/api/workspaces/w1/connections")).toHaveLength(1));
    const post = m.find("POST", "/api/workspaces/w1/connections")[0];
    expect(post.body).toEqual({ kind: "github", label: "github", credential: { token: SECRET } });
    expect(post.headers["Idempotency-Key"]).toBeTruthy();
    expect(JSON.stringify(localStorage) + JSON.stringify(sessionStorage)).not.toContain(SECRET);
    expect(document.body.innerHTML).not.toContain(SECRET);
    expect(await screen.findByRole("group", { name: "github" })).toBeInTheDocument();
  });

  it("shows server health, the reason and a named validation retry", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/harnesses", { json: { items: HARNESSES } }],
      [
        "GET",
        "/api/workspaces/w1/connections",
        {
          json: {
            items: [
              connection({
                kind: "inference_api",
                label: "DeepSeek",
                health: "reauth_required",
                health_reason: "inference_endpoint_or_model_rejected",
                config: {
                  endpoints: { openai_chat: "https://api.example.test", anthropic_messages: "https://api.example.test/anthropic" },
                  model: "flash",
                  models: ["flash", "pro"],
                },
                validation: {
                  status: "invalid",
                  observed_at: "2026-01-01T00:00:00Z",
                  details: { endpoints: { openai_chat: { status: "ready", http_status: 200 }, anthropic_messages: { status: "invalid", http_status: 404 } } },
                },
              }),
            ],
          },
        },
      ],
      ["POST", "/api/connections/c_inference_api/validations", { status: 202, json: { job_id: "j1" } }],
    ]);
    await renderApp(<ConnectionsPage />, m.fetch);
    const card = await screen.findByRole("group", { name: "DeepSeek" });
    expect(card).toHaveTextContent("Re-authentication required");
    expect(card).toHaveTextContent("The provider did not accept this base URL, protocol or model.");
    expect(card).toHaveTextContent("inference_endpoint_or_model_rejected");
    expect(card).toHaveTextContent("https://api.example.test/anthropic");
    expect(card).toHaveTextContent("check failed (404)");
    expect(card).toHaveTextContent("2 models available");
    await userEvent.click(within(card).getByRole("button", { name: "Retry validation" }));
    await waitFor(() => expect(m.find("POST", "/api/connections/c_inference_api/validations")).toHaveLength(1));
  });

  it("adds a generic inference key with per-protocol base URLs and clears the key", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/harnesses", { json: { items: HARNESSES } }],
      ["GET", "/api/workspaces/w1/connections", { json: { items: [] } }],
      ["POST", "/api/workspaces/w1/connections", { status: 201, json: connection({ kind: "inference_api" }) }],
    ]);
    await renderApp(<ConnectionsPage />, m.fetch);
    const form = await screen.findByRole("form", { name: "Add Inference API key connection" });
    const f = within(form);
    // The DeepSeek preset is a starting point: every protocol the provider speaks is prefilled.
    expect((f.getByLabelText("OpenAI Chat Completions") as HTMLInputElement).value).toBe("https://api.deepseek.com");
    expect((f.getByLabelText("Anthropic Messages") as HTMLInputElement).value).toBe("https://api.deepseek.com/anthropic");
    expect((f.getByLabelText("Default model") as HTMLInputElement).value).toBe("deepseek-flash");
    expect(form).toHaveTextContent("Used by OpenCode, Grok Build");
    expect(form).toHaveTextContent("Used by OpenCode, Claude Code");
    expect(f.getByRole("button", { name: "Connect" })).toBeDisabled();

    await userEvent.selectOptions(f.getByLabelText("Provider"), "custom");
    expect((f.getByLabelText("OpenAI Chat Completions") as HTMLInputElement).value).toBe("");
    await userEvent.type(f.getByLabelText("Label"), "My gateway");
    const key = f.getByLabelText("API key") as HTMLInputElement;
    expect(key.type).toBe("password");
    await userEvent.type(key, SECRET);
    await userEvent.type(f.getByLabelText("Default model"), "vendor/model-1");
    await userEvent.type(f.getByLabelText("More models (optional)"), "vendor/model-2, vendor/model-3");
    expect(f.getByRole("button", { name: "Connect" })).toBeDisabled();
    await userEvent.type(f.getByLabelText("OpenAI Responses"), "gateway.example.test/v1");
    expect(form).toHaveTextContent("Base URLs must start with https://.");
    await userEvent.clear(f.getByLabelText("OpenAI Responses"));
    await userEvent.type(f.getByLabelText("OpenAI Responses"), "https://gateway.example.test/v1");
    await userEvent.click(f.getByRole("button", { name: "Connect" }));

    await waitFor(() => expect(m.find("POST", "/api/workspaces/w1/connections")).toHaveLength(1));
    expect(m.find("POST", "/api/workspaces/w1/connections")[0].body).toEqual({
      kind: "inference_api",
      label: "My gateway",
      credential: {
        api_key: SECRET,
        model: "vendor/model-1",
        endpoints: { openai_responses: "https://gateway.example.test/v1" },
        models: ["vendor/model-2", "vendor/model-3"],
      },
    });
    expect(JSON.stringify(localStorage) + JSON.stringify(sessionStorage)).not.toContain(SECRET);
    expect(document.body.innerHTML).not.toContain(SECRET);
  });

  it("rotates a key without retyping endpoints and confirms before disconnecting", async () => {
    const existing = connection({
      kind: "inference_api",
      label: "DeepSeek",
      version: 4,
      config: { endpoints: { openai_chat: "https://api.example.test" }, model: "flash", models: ["flash"] },
    });
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/harnesses", { json: { items: HARNESSES } }],
      ["GET", "/api/workspaces/w1/connections", { json: { items: [existing] } }],
      ["POST", "/api/connections/c_inference_api/credential-versions", { status: 201, json: existing }],
      ["DELETE", "/api/connections/c_inference_api", { json: { ...existing, state: "revoked" } }],
    ]);
    await renderApp(<ConnectionsPage />, m.fetch);
    const card = await screen.findByRole("group", { name: "DeepSeek" });
    await userEvent.click(within(card).getByRole("button", { name: "Replace credential…" }));
    const form = within(card).getByRole("form", { name: "Replace Inference API key credential" });
    expect((within(form).getByLabelText("OpenAI Chat Completions") as HTMLInputElement).value).toBe("https://api.example.test");
    await userEvent.type(within(form).getByLabelText("API key"), SECRET);
    await userEvent.click(within(form).getByRole("button", { name: "Replace credential" }));
    await waitFor(() => expect(m.find("POST", "/api/connections/c_inference_api/credential-versions")).toHaveLength(1));
    expect(m.find("POST", "/api/connections/c_inference_api/credential-versions")[0].body).toEqual({
      credential: { api_key: SECRET, model: "flash", endpoints: { openai_chat: "https://api.example.test" } },
      expected_version: 4,
    });

    await userEvent.click(within(card).getByRole("button", { name: "Disconnect" }));
    expect(m.find("DELETE", "/api/connections/c_inference_api")).toHaveLength(0);
    await userEvent.click(within(card).getByRole("button", { name: "Confirm disconnect" }));
    await waitFor(() => expect(m.find("DELETE", "/api/connections/c_inference_api")).toHaveLength(1));
  });

  it("lists retired vendor connections read-only and offers no way to add one", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/harnesses", { json: { items: HARNESSES } }],
      [
        "GET",
        "/api/workspaces/w1/connections",
        { json: { items: [connection({ kind: "opencode_zen", label: "old zen", legacy: true })] } },
      ],
    ]);
    await renderApp(<ConnectionsPage />, m.fetch);
    const retired = await screen.findByRole("region", { name: "Retired connections" });
    const card = within(retired).getByRole("group", { name: "old zen" });
    expect(within(card).queryByRole("button", { name: "Replace credential…" })).toBeNull();
    expect(within(card).queryByRole("button", { name: "Validate" })).toBeNull();
    expect(within(card).getByRole("button", { name: "Disconnect" })).toBeInTheDocument();
    const html = document.body.innerHTML;
    for (const gone of ["OpenCode Zen API key", "auth.json", "ChatGPT plan", "Connect Codex", "soren-labs"]) {
      expect(html).not.toContain(gone);
    }
  });
});

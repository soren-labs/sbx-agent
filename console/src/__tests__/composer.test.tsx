import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { NewSession } from "../features/sessions/NewSession";
import { modelOptions, pickBackend, pickDefaultModel } from "../features/sessions/defaults";
import type { ModelsView } from "../api/types";
import { connection, identityRoutes, mockFetch, renderApp, session } from "../test/helpers";

const MODELS: ModelsView = {
  provider_id: "opencode",
  preferred_model: "big-pickle",
  connections: [
    {
      connection_id: "c_zen",
      label: "zen",
      health: "ready",
      models: [
        { id: "paid-pro", free: false },
        { id: "big-pickle", free: true },
        { id: "other-free", free: true },
      ],
    },
  ],
};

describe("defaults", () => {
  it("prefers the server's preferred model only when it is free", () => {
    expect(pickDefaultModel(MODELS)).toBe("big-pickle");
    expect(pickDefaultModel({ ...MODELS, preferred_model: "paid-pro" })).toBe("big-pickle");
    expect(pickDefaultModel({ ...MODELS, preferred_model: null })).toBe("big-pickle");
    expect(pickDefaultModel(undefined)).toBeUndefined();
    expect(modelOptions(MODELS).map((o) => o.id)).toEqual(["paid-pro", "big-pickle", "other-free"]);
  });
  it("picks Modal when a Modal connection exists", () => {
    expect(pickBackend([connection({ kind: "modal" })])).toBe("modal");
    expect(pickBackend([connection({ kind: "github" })])).toBe("local");
  });
});

describe("new Session composer", () => {
  const routes = (calls?: { created?: unknown }) =>
    mockFetch([
      ...identityRoutes,
      ["GET", "/api/workspaces/w1/connections", { json: { items: [connection({ kind: "modal" }), connection({ kind: "github" }), connection({ kind: "opencode_zen", id: "c_zen" })] } }],
      ["GET", "/api/models", { json: MODELS }],
      ["GET", "/api/workspaces/w1/projects", { json: { items: [] } }],
      ["POST", "/api/workspaces/w1/sessions", (c) => {
        if (calls) calls.created = c.body;
        return { status: 202, json: { session_id: "s9", turn_id: "t1", session: session({ id: "s9" }), event_watermark: 3 } };
      }],
    ]);

  it("defaults to opencode, the preferred free model and Modal — with no Codex connection", async () => {
    const m = routes();
    await renderApp(
      <Routes>
        <Route path="/" element={<NewSession />} />
        <Route path="/sessions/:id" element={<div>opened session</div>} />
      </Routes>,
      m.fetch,
    );
    const optionsToggle = screen.getByText("Session options");
    expect(optionsToggle.closest("details")).not.toHaveAttribute("open");
    await userEvent.click(optionsToggle);
    expect(optionsToggle.closest("details")).toHaveAttribute("open");
    const model = (await screen.findByLabelText("Model")) as HTMLSelectElement;
    await waitFor(() => expect(model.value).toBe("big-pickle"));
    expect((screen.getByLabelText("Compute") as HTMLSelectElement).value).toBe("modal");
    expect((screen.getByLabelText("Harness") as HTMLSelectElement).value).toBe("opencode");
    expect(screen.getByRole("option", { name: "big-pickle (free)" })).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Task"), "Fix the failing check");
    await userEvent.type(screen.getByLabelText("Repository"), "acme/app");
    await userEvent.click(optionsToggle);
    expect(optionsToggle.closest("details")).not.toHaveAttribute("open");
    await userEvent.click(screen.getByLabelText("Task"));
    await userEvent.keyboard("{Control>}{Enter}{/Control}");

    await screen.findByText("opened session");
    const post = m.find("POST", "/api/workspaces/w1/sessions")[0];
    expect(post.body).toEqual({
      harness: { provider_id: "opencode", model: "big-pickle" },
      executor: { backend: "modal" },
      repository: { full_name: "acme/app", base_ref: "main" },
      message: { content: "Fix the failing check" },
    });
    expect(post.headers["Idempotency-Key"]).toBeTruthy();
    expect(JSON.stringify(post.body)).not.toContain("codex");
  });
});

import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ConnectionForm } from "../features/connections/ConnectionForm";
import { ConnectionsPage } from "../features/connections/ConnectionsPage";
import { connection, identityRoutes, mockFetch, renderApp } from "../test/helpers";

const SECRET = "ghp_SUPERSECRETVALUE";

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

  it("shows server health, the preferred free model and a named validation retry", async () => {
    const m = mockFetch([
      ...identityRoutes,
      [
        "GET",
        "/api/workspaces/w1/connections",
        {
          json: {
            items: [
              connection({
                kind: "opencode_zen",
                label: "zen",
                health: "degraded",
                health_reason: "rate_limited",
                catalog: { preferred_model: "big-pickle", models: [{ id: "big-pickle", free: true }, { id: "paid-1", free: false }] },
              }),
            ],
          },
        },
      ],
    ]);
    await renderApp(<ConnectionsPage />, m.fetch);
    const card = await screen.findByRole("group", { name: "zen" });
    expect(card).toHaveTextContent("Degraded");
    expect(card).toHaveTextContent("rate_limited");
    expect(card).toHaveTextContent("big-pickle");
    expect(card).toHaveTextContent("free");
    expect(screen.getByRole("button", { name: "Retry validation" })).toBeInTheDocument();
    expect(screen.getByText("Codex (optional)")).toBeInTheDocument();
  });
});

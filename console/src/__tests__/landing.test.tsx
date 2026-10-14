import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import { identityRoutes, mockFetch, renderApp } from "../test/helpers";

const anonymous = { status: 401, json: { error: { code: "unauthenticated" } } };

describe("public landing page", () => {
  it("is shown at the root to visitors who are not signed in", async () => {
    const m = mockFetch([["GET", "/api/me", anonymous]]);
    await renderApp(<App />, m.fetch, "/");
    expect(
      await screen.findByRole("heading", { level: 1, name: "Coding agents that ship reviewed pull requests." }),
    ).toBeInTheDocument();
    const header = screen.getByRole("banner");
    expect(within(header).getByRole("link", { name: "Get started" })).toHaveAttribute("href", "/register");
    expect(within(header).getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/login");
    // No product data is requested for anonymous visitors.
    expect(m.calls.map((c) => c.path)).toEqual(["/api/me"]);
  });

  it("switches language in place", async () => {
    const m = mockFetch([["GET", "/api/me", anonymous]]);
    await renderApp(<App />, m.fetch, "/");
    await userEvent.click(await screen.findByRole("button", { name: "中文" }));
    expect(await screen.findByRole("heading", { level: 1, name: "交付经过审阅的 Pull Request 的编码 Agent" })).toBeInTheDocument();
  });

  it("keeps deep links behind sign-in", async () => {
    const m = mockFetch([["GET", "/api/me", anonymous]]);
    await renderApp(<App />, m.fetch, "/sessions");
    expect(await screen.findByRole("form", { name: "Sign in" })).toBeInTheDocument();
  });

  it("shows the workbench home, not the landing page, when signed in", async () => {
    const m = mockFetch([...identityRoutes]);
    await renderApp(<App />, m.fetch, "/");
    expect(await screen.findByRole("form", { name: "New Session" })).toBeInTheDocument();
    expect(screen.queryByText("Coding agents that ship reviewed pull requests.")).toBeNull();
  });
});

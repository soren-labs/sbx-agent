import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import { identityRoutes, ME, mockFetch, renderApp, session } from "../test/helpers";

const anonymous = { status: 401, json: { error: { code: "unauthenticated" } } };

describe("restored auth and shell interactions", () => {
  it("keeps protected destination and submits the unified login form with write-only password", async () => {
    const m = mockFetch([
      ["GET", "/api/me", anonymous],
      ["POST", "/api/auth/login", { json: { ...ME, csrf_token: "REDACTED", expires_at: "2099-01-01T00:00:00Z" } }],
      ["GET", "/api/api-keys", { json: { items: [] } }],
    ]);
    await renderApp(<App />, m.fetch, "/settings");
    await screen.findByRole("heading", { name: "Sign in" });
    expect(document.documentElement.dataset.theme).toBe("dark");
    await userEvent.type(screen.getByLabelText("Email"), "dev@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "REDACTED");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("heading", { name: "Settings", level: 1 });
    expect(m.find("POST", "/api/auth/login")[0].body).toEqual({ email: "dev@example.com", password: "REDACTED" });
    expect(localStorage.getItem("password")).toBeNull();
  });

  it("supports registration and verification through the same accessible auth layout", async () => {
    const m = mockFetch([
      ["GET", "/api/me", anonymous],
      ["POST", "/api/auth/register", { json: { status: "pending" } }],
    ]);
    await renderApp(<App />, m.fetch, "/login");
    await userEvent.click(await screen.findByRole("link", { name: "Create account" }));
    await userEvent.type(screen.getByLabelText("Email"), "dev@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "REDACTED");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("status")).toHaveTextContent("dev@example.com");
    expect(m.find("POST", "/api/auth/register")[0].headers["Idempotency-Key"]).toBeTruthy();
  });

  it("verifies email without replacing the unified token contract", async () => {
    const m = mockFetch([
      ["GET", "/api/me", anonymous],
      ["POST", "/api/auth/email-verifications", { json: { status: "verified" } }],
    ]);
    await renderApp(<App />, m.fetch, "/verify-email?token=REDACTED");
    expect(await screen.findByRole("status")).toHaveTextContent("verified");
    expect(m.find("POST", "/api/auth/email-verifications")[0].body).toEqual({ token: "REDACTED" });
  });

  it("shell navigation reflects the route and real Session state", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/workspaces/w1/connections", { json: { items: [] } }],
      ["GET", "/api/workspaces/w1/projects", { json: { items: [] } }],
      [
        "GET",
        "/api/workspaces/w1/sessions",
        {
          json: {
            items: [
              session({ id: "s1", title: "", activity: "awaiting_input", updated_at: new Date().toISOString() }),
              session({ id: "s2", title: "Fix the PR template", activity: "attention", updated_at: new Date().toISOString() }),
            ],
            next_cursor: null,
          },
        },
      ],
    ]);
    await renderApp(<App />, m.fetch, "/projects");
    const nav = (await screen.findAllByRole("navigation"))[0];
    // Only the current section is marked; there is no invented "Review" queue.
    expect(within(nav).getByRole("link", { name: "Projects" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Sessions" })).not.toHaveAttribute("aria-current");
    expect(within(nav).queryByText("Review")).toBeNull();
    // Untitled Sessions are still reachable, and status comes from server activity alone.
    const untitled = await within(nav).findByRole("link", { name: /Untitled Session/ });
    expect(untitled).toHaveTextContent("Waiting for you");
    const titled = within(nav).getByRole("link", { name: /Fix the PR template/ });
    expect(titled).toHaveTextContent("Needs attention");
    expect(titled).not.toHaveTextContent("PR ready");
    await userEvent.type(within(nav).getByLabelText("Search sessions"), "untitled");
    expect(within(nav).queryByRole("link", { name: /Fix the PR template/ })).toBeNull();
    expect(within(nav).getByRole("link", { name: /Untitled Session/ })).toBeInTheDocument();
  });

  it("keeps desktop/mobile navigation, theme persistence and logout working", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/workspaces/w1/sessions", { json: { items: [], next_cursor: null } }],
      ["GET", "/api/api-keys", { json: { items: [] } }],
      ["POST", "/api/auth/logout", { status: 204 }],
    ]);
    await renderApp(<App />, m.fetch, "/sessions");
    const navs = screen.getAllByRole("navigation");
    expect(navs).toHaveLength(2);
    expect(within(navs[0]).getByRole("link", { name: "Sessions" })).toHaveAttribute("aria-current", "page");
    await userEvent.click(within(navs[1]).getByRole("link", { name: "Settings" }));
    await userEvent.selectOptions(screen.getByLabelText("Theme"), "light");
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("light"));
    expect(localStorage.getItem("sbx.console.theme")).toBe("light");
    await userEvent.click(screen.getAllByRole("button", { name: "Sign out" })[0]);
    await screen.findByRole("heading", { name: "Sign in" });
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(m.find("POST", "/api/auth/logout")).toHaveLength(1);
  });
});

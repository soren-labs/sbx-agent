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
        (call) => {
          const all = [
            session({ id: "s1", title: "", activity: "awaiting_input", updated_at: new Date().toISOString() }),
            session({ id: "s2", title: "Fix the PR template", activity: "attention", updated_at: new Date().toISOString() }),
          ];
          // Like the server: q matches stored titles across the whole workspace.
          const q = (call.query.get("q") ?? "").toLowerCase();
          return { json: { items: all.filter((s) => s.title.toLowerCase().includes(q)), next_cursor: null } };
        },
      ],
    ]);
    await renderApp(<App />, m.fetch, "/projects");
    const aside = await screen.findByRole("complementary");
    const nav = within(aside).getByRole("navigation");
    // Only the current section is marked; there is no invented "Review" queue.
    expect(within(nav).getByRole("link", { name: "Projects" })).toHaveAttribute("aria-current", "page");
    expect(within(aside).getByRole("link", { name: "Sessions" })).not.toHaveAttribute("aria-current");
    expect(within(aside).queryByText("Review")).toBeNull();
    // Untitled Sessions are still reachable, and status comes from server activity alone.
    const untitled = await within(aside).findByRole("link", { name: /Untitled session/ });
    expect(untitled).toHaveTextContent("Waiting for you");
    const titled = within(aside).getByRole("link", { name: /Fix the PR template/ });
    expect(titled).toHaveTextContent("Needs attention");
    expect(titled).not.toHaveTextContent("PR ready");
    // The default list asks for open Sessions only (issue #228 AUD13).
    expect(m.find("GET", "/api/workspaces/w1/sessions")[0].query.get("lifecycle")).toBe("open");
    // The filter opens from the sessions-section search action and searches on the server,
    // so Sessions beyond the loaded page are found too (AUD14).
    await userEvent.click(within(aside).getAllByRole("button", { name: "Search sessions" }).at(-1)!);
    await userEvent.type(within(aside).getByLabelText("Filter sessions…"), "PR template");
    await waitFor(() => expect(within(aside).queryByRole("link", { name: /Untitled session/ })).toBeNull());
    expect(within(aside).getByRole("link", { name: /Fix the PR template/ })).toBeInTheDocument();
    expect(m.find("GET", "/api/workspaces/w1/sessions").some((c) => c.query.get("q") === "PR template")).toBe(true);
  });

  it("shows harness · model for quiet Sessions and tags Closed only once it is opted in", async () => {
    const closed = session({
      id: "s9",
      lifecycle: "closed",
      activity: "idle",
      title: "Old fix",
      harness: { provider_id: "codex", model: "gpt-5" },
      updated_at: new Date().toISOString(),
    });
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/workspaces/w1/connections", { json: { items: [] } }],
      ["GET", "/api/workspaces/w1/projects", { json: { items: [] } }],
      [
        "GET",
        "/api/workspaces/w1/sessions",
        (call) => {
          const wanted = (call.query.get("lifecycle") ?? "open,archived,closed").split(",");
          const all = [session({ id: "s1", title: "Open one", updated_at: new Date().toISOString() }), closed];
          return { json: { items: all.filter((s) => wanted.includes(s.lifecycle)), next_cursor: null } };
        },
      ],
    ]);
    await renderApp(<App />, m.fetch, "/");
    const aside = await screen.findByRole("complementary");
    // Quiet sessions keep harness · model · time; no "Closed" label in the meta.
    const open = await within(aside).findByRole("link", { name: /Open one/ });
    expect(open).toHaveTextContent("OpenCode · big-pickle");
    await userEvent.click(within(aside).getByRole("button", { name: "Session list options" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Show closed" }));
    const item = await within(aside).findByRole("link", { name: /Old fix/ });
    expect(item).toHaveTextContent("Codex · gpt-5");
    expect(within(item).getByText("Closed")).toHaveClass("session-item__tag");
  });

  it("keeps navigation, theme persistence and logout working", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/workspaces/w1/sessions", { json: { items: [], next_cursor: null } }],
      ["GET", "/api/api-keys", { json: { items: [] } }],
      ["POST", "/api/auth/logout", { status: 204 }],
    ]);
    await renderApp(<App />, m.fetch, "/sessions");
    const navs = screen.getAllByRole("navigation");
    expect(navs).toHaveLength(1);
    const aside = navs[0].closest("aside")!;
    expect(within(aside).getByRole("link", { name: "Sessions" })).toHaveAttribute("aria-current", "page");
    await userEvent.click(within(aside).getByRole("link", { name: "Settings" }));
    await screen.findByRole("heading", { name: "Settings", level: 1 });
    // Theme is chosen on the General tab via radio cards.
    await userEvent.click(await screen.findByRole("radio", { name: /^Light/ }));
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("light"));
    expect(localStorage.getItem("sbx.console.theme")).toBe("light");
    await userEvent.click(within(aside).getByRole("button", { name: "Account" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Sign out" }));
    await screen.findByRole("heading", { name: "Sign in" });
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(m.find("POST", "/api/auth/logout")).toHaveLength(1);
  });
});

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SettingsPage } from "../features/settings/SettingsPage";
import { identityRoutes, mockFetch, renderApp } from "../test/helpers";

const KEY = "sbx_key_REDACTED_plaintext_shown_once";

describe("Settings API keys", () => {
  it("creates a real key, shows it once with a copy action, and revokes only after confirmation", async () => {
    let items: Record<string, unknown>[] = [];
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/api-keys", () => ({ json: { items } })],
      [
        "POST",
        "/api/api-keys",
        (call) => {
          const created = { id: "k1", name: (call.body as { name: string }).name, prefix: "sbx_key_REDA", scopes: ["*"], created_at: "2026-10-09T00:00:00Z", revoked_at: null };
          items = [created];
          return { status: 201, json: { ...created, key: KEY } };
        },
      ],
      [
        "DELETE",
        "/api/api-keys/k1",
        () => {
          items = [{ ...items[0], revoked_at: "2026-10-09T01:00:00Z" }];
          return { json: items[0] };
        },
      ],
    ]);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    await renderApp(<SettingsPage />, m.fetch, "/settings?tab=keys");
    expect(await screen.findByText("No API keys.")).toBeInTheDocument();
    // The create form lives in a dialog behind the "Create key" action.
    await userEvent.click(screen.getAllByRole("button", { name: "Create key" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "Create key" });
    await userEvent.type(within(dialog).getByLabelText("Key name"), "laptop CLI");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create key" }));

    const reveal = await screen.findByTestId("key-reveal");
    expect(reveal).toHaveTextContent(KEY);
    expect(reveal).toHaveTextContent("Authorization: Bearer sbx_key_REDA…");
    expect(reveal).toHaveTextContent("You won't see this again");
    await userEvent.click(within(reveal).getByRole("button", { name: "Copy key" }));
    expect(writeText).toHaveBeenCalledWith(KEY);
    expect(await within(reveal).findByRole("button", { name: "Copied" })).toBeInTheDocument();
    expect(JSON.stringify(localStorage) + JSON.stringify(sessionStorage)).not.toContain(KEY);
    // Dismissing discards the plaintext for good: the list only ever has the prefix.
    await userEvent.click(within(reveal).getByRole("button", { name: "I have copied it" }));
    expect(document.body.innerHTML).not.toContain(KEY);
    const list = screen.getByRole("list", { name: "API keys" });
    expect(list).toHaveTextContent("laptop CLI");
    expect(list).toHaveTextContent("sbx_key_REDA…");

    await userEvent.click(within(list).getByRole("button", { name: "Revoke" }));
    expect(m.find("DELETE", "/api/api-keys/k1")).toHaveLength(0);
    const confirm = await screen.findByRole("dialog", { name: "Revoke API key" });
    await userEvent.click(within(confirm).getByRole("button", { name: "Revoke key" }));
    await waitFor(() => expect(m.find("DELETE", "/api/api-keys/k1")).toHaveLength(1));
    await waitFor(() => expect(screen.getByRole("list", { name: "API keys" })).toHaveTextContent("revoked"));
    expect(within(screen.getByRole("list", { name: "API keys" })).queryByRole("button", { name: "Revoke" })).toBeNull();
  });
});

type Call = { body: unknown; headers: Record<string, string> };
const view = (over: Record<string, unknown> = {}) => ({
  id: "k1",
  name: "ci",
  prefix: "sbx_key_REDA",
  scopes: ["write"],
  created_at: "2026-10-16T00:00:00Z",
  revoked_at: null,
  ...over,
});

async function openCreate() {
  await userEvent.click((await screen.findAllByRole("button", { name: "Create key" }))[0]);
  return screen.findByRole("dialog", { name: "Create key" });
}

describe("API key scopes and retry-safe issuance (#230)", () => {
  it("defaults to Read & write, explains each scope and shows scopes in the list", async () => {
    let items: Record<string, unknown>[] = [];
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/api-keys", () => ({ json: { items } })],
      [
        "POST",
        "/api/api-keys",
        (call) => {
          const b = call.body as { name: string; scopes: string[] };
          const created = view({ id: `k${items.length + 1}`, name: b.name, scopes: b.scopes });
          items = [created, ...items];
          return { status: 201, json: { ...created, key: KEY } };
        },
      ],
    ]);
    await renderApp(<SettingsPage />, m.fetch, "/settings?tab=keys");
    let dialog = await openCreate();
    const access = within(dialog).getByRole("group", { name: "Access" });
    expect(within(access).getByRole("radio", { name: /Read & write/ })).toBeChecked();
    expect(access).toHaveTextContent("Cannot create or revoke API keys");
    expect(access).toHaveTextContent("Cannot change anything");
    expect(access).toHaveTextContent("including creating and revoking API keys");
    await userEvent.type(within(dialog).getByLabelText("Key name"), "ci");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create key" }));
    expect(await screen.findByTestId("key-reveal")).toHaveTextContent("Access: Read & write.");
    expect(m.find("POST", "/api/api-keys")[0].body).toEqual({ name: "ci", scopes: ["write"] });

    dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText("Key name"), "dashboard");
    await userEvent.click(within(dialog).getByRole("radio", { name: /Read only/ }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Create key" }));
    await waitFor(() => expect(m.find("POST", "/api/api-keys")).toHaveLength(2));
    expect(m.find("POST", "/api/api-keys")[1].body).toEqual({ name: "dashboard", scopes: ["read"] });
    const list = await screen.findByRole("list", { name: "API keys" });
    await waitFor(() => expect(within(list).getByRole("listitem", { name: "dashboard" })).toHaveTextContent("Read only"));
    expect(within(list).getByRole("listitem", { name: "ci" })).toHaveTextContent("Read & write");
  });

  it("keeps one Idempotency-Key across an uncertain outcome and gets the same key back", async () => {
    const posts: Call[] = [];
    let fail = 3; // the client's own retries (2) plus the first attempt, all 503
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/api-keys", { json: { items: [] } }],
      [
        "POST",
        "/api/api-keys",
        (call) => {
          posts.push(call as Call);
          if (fail-- > 0) return { status: 503, json: { error: { code: "bad_gateway" } } };
          return { status: 201, json: { ...view(), key: KEY, replayed: true } };
        },
      ],
    ]);
    await renderApp(<SettingsPage />, m.fetch, "/settings?tab=keys");
    const dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText("Key name"), "ci");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create key" }));
    expect(await within(dialog).findByText("The key may have been created")).toBeInTheDocument();
    // The request body cannot change while its outcome is unknown.
    expect(within(dialog).getByLabelText("Key name")).toBeDisabled();
    expect(within(dialog).getByRole("radio", { name: /Read only/ })).toBeDisabled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("key-reveal")).toHaveTextContent(KEY);
    expect(posts).toHaveLength(4);
    expect(new Set(posts.map((p) => p.headers["Idempotency-Key"])).size).toBe(1);
  });

  it("offers revoke-and-reissue when an earlier attempt's secret is gone", async () => {
    const posts: Call[] = [];
    let items: Record<string, unknown>[] = [view()];
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/api-keys", () => ({ json: { items } })],
      [
        "POST",
        "/api/api-keys",
        (call) => {
          posts.push(call as Call);
          if (posts.length === 1)
            return {
              status: 409,
              json: {
                error: {
                  code: "secret_unavailable",
                  message: "already created",
                  details: { api_key: view(), reason: "replay_expired" },
                },
              },
            };
          const created = view({ id: "k2" });
          items = [created, ...items];
          return { status: 201, json: { ...created, key: KEY } };
        },
      ],
      [
        "DELETE",
        "/api/api-keys/k1",
        () => {
          items = items.map((k) => (k.id === "k1" ? { ...k, revoked_at: "2026-10-16T01:00:00Z" } : k));
          return { json: { ...view(), revoked_at: "2026-10-16T01:00:00Z" } };
        },
      ],
    ]);
    await renderApp(<SettingsPage />, m.fetch, "/settings?tab=keys");
    const dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText("Key name"), "ci");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create key" }));
    const orphan = await screen.findByTestId("key-orphan");
    expect(orphan).toHaveTextContent("“ci” was created, but its secret can no longer be shown");
    await userEvent.click(within(orphan).getByRole("button", { name: "Revoke it and create a new key" }));
    await waitFor(() => expect(m.find("DELETE", "/api/api-keys/k1")).toHaveLength(1));
    const again = await screen.findByRole("dialog", { name: "Create key" });
    expect(within(again).getByLabelText("Key name")).toHaveValue("ci");
    await userEvent.click(within(again).getByRole("button", { name: "Create key" }));
    expect(await screen.findByTestId("key-reveal")).toHaveTextContent(KEY);
    expect(posts).toHaveLength(2);
    expect(posts[1].headers["Idempotency-Key"]).not.toBe(posts[0].headers["Idempotency-Key"]);
  });

  it("says a key may exist when the dialog is closed with an unknown outcome", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/api-keys", { json: { items: [] } }],
      ["POST", "/api/api-keys", { status: 504, json: { error: { code: "gateway_timeout" } } }],
    ]);
    await renderApp(<SettingsPage />, m.fetch, "/settings?tab=keys");
    const dialog = await openCreate();
    await userEvent.type(within(dialog).getByLabelText("Key name"), "nightly");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create key" }));
    await within(dialog).findByText("The key may have been created");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(await screen.findByText(/Creating “nightly” got no response/)).toBeInTheDocument();
  });
});

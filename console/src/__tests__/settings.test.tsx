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
    await renderApp(<SettingsPage />, m.fetch);
    expect(await screen.findByText("No API keys.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Key name"), "laptop CLI");
    await userEvent.click(screen.getByRole("button", { name: "Create key" }));

    const reveal = await screen.findByTestId("key-reveal");
    expect(reveal).toHaveTextContent(KEY);
    expect(reveal).toHaveTextContent("Authorization: Bearer sbx_key_REDA…");
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
    await userEvent.click(within(list).getByRole("button", { name: "Revoke key" }));
    await waitFor(() => expect(m.find("DELETE", "/api/api-keys/k1")).toHaveLength(1));
    await waitFor(() => expect(list).toHaveTextContent("revoked"));
    expect(within(list).queryByRole("button", { name: "Revoke" })).toBeNull();
  });
});

import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { afterEach, it, expect, vi } from "vitest";
import { App } from "./App";
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
it("offers the four-class setup without Codex and never stores a submitted provider key", async () => {
  const calls: { url: string; options?: RequestInit }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options?: RequestInit) => {
      calls.push({ url, options });
      const data =
        url === "/api/me"
          ? {
              id: "user_one",
              email: "one@example.test",
              verified_at: "2026-10-05",
              workspace_ids: ["workspace_one"],
            }
          : { items: [] };
      return { ok: true, json: async () => data };
    }),
  );
  render(<App />);
  expect(await screen.findByText("Minimum setup")).toBeInTheDocument();
  expect(screen.getByText("✓ Email and password")).toBeInTheDocument();
  expect(screen.queryByText("Codex login")).not.toBeInTheDocument();
  fireEvent.click(
    screen.getByRole("button", { name: /^Connections$/ }),
  );
  const field = await screen.findByLabelText("api key");
  expect(field).toHaveAttribute("type", "password");
  fireEvent.change(field, { target: { value: "REDACTED" } });
  fireEvent.click(
    screen.getByRole("button", { name: "Store encrypted and validate" }),
  );
  await waitFor(() => expect(field).toHaveValue(""));
  expect(
    calls.some((c) => c.options?.body?.toString().includes("REDACTED")),
  ).toBe(true);
  expect(localStorage.length).toBe(0);
});

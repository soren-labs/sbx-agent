import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SessionsPage } from "../pages/SessionsPage";
import { NewSessionPage } from "../pages/NewSessionPage";
import { PrototypeApp } from "../prototype/PrototypeApp";
import { SESSIONS } from "../api/fixtures";
import { makeApi, renderApp } from "../test/helpers";

describe("SessionsPage", () => {
  it("lists sessions with status pills, provider, repo", async () => {
    renderApp(<SessionsPage />, { api: makeApi() });
    const list = await screen.findByTestId("session-list");
    for (const s of SESSIONS) {
      expect(within(list).getByText(s.title)).toBeInTheDocument();
    }
    expect(within(list).getByTestId("pill-running")).toBeInTheDocument();
    expect(within(list).getByTestId("pill-queued")).toBeInTheDocument();
    expect(within(list).getByTestId("pill-failed")).toBeInTheDocument();
    expect(within(list).getAllByText("soren-labs/sbx-browser").length).toBeGreaterThan(0);
  });

  it("filters live vs ended", async () => {
    renderApp(<SessionsPage />, { api: makeApi() });
    await screen.findByTestId("session-list");
    await userEvent.click(screen.getByRole("tab", { name: "Live" }));
    expect(screen.queryByText("Explain the retry backoff in the SSE client")).not.toBeInTheDocument();
    expect(screen.getByText("Migrate the settings store to the new schema")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Ended" }));
    expect(screen.getByText("Explain the retry backoff in the SSE client")).toBeInTheDocument();
    expect(screen.queryByText("Migrate the settings store to the new schema")).not.toBeInTheDocument();
  });

  it("filters by search query", async () => {
    renderApp(<SessionsPage />, { api: makeApi() });
    await screen.findByTestId("session-list");
    await userEvent.type(screen.getByRole("searchbox"), "health-check");
    expect(screen.getByText(/health-check endpoint/)).toBeInTheDocument();
    expect(screen.queryByText(/settings store/)).not.toBeInTheDocument();
  });

  it("shows empty state when nothing matches", async () => {
    renderApp(<SessionsPage />, { api: makeApi() });
    await screen.findByTestId("session-list");
    await userEvent.type(screen.getByRole("searchbox"), "zzz-nothing");
    await waitFor(() =>
      expect(screen.getByText("No sessions match.")).toBeInTheDocument(),
    );
  });
});

describe("NewSessionPage", () => {
  it("shows the composer and recent sessions — not a dashboard", async () => {
    renderApp(<NewSessionPage />, { api: makeApi() });
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(await screen.findByTestId(`session-card-${SESSIONS[0].id}`)).toBeInTheDocument();
    expect(screen.queryByText(/dashboard/i)).not.toBeInTheDocument();
    // no Tasks/Agents/Workflows/Artifacts anywhere
    expect(document.body.innerHTML).not.toMatch(/Tasks|Agents|Workflows|Artifacts/);
  });

  it("create → optimistic navigation to the session shell", async () => {
    const api = makeApi();
    renderApp(<PrototypeApp />, { api });
    const prompt=await screen.findByRole("textbox",{name:"Session task"});
    await userEvent.type(prompt,"Ship it");
    await userEvent.click(screen.getByRole("button",{name:"Start session"}));
    expect(await screen.findByRole("textbox",{name:"Follow-up message"})).toBeInTheDocument();
    expect(screen.getByRole("heading",{name:"Ship it"})).toBeInTheDocument();

  });
});

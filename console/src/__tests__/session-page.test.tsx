import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { SessionPage } from "../features/sessions/SessionPage";
import { identityRoutes, mockFetch, renderApp, session } from "../test/helpers";

const enc = new TextEncoder();
const frame = (seq: number, type: string, payload: Record<string, unknown>) =>
  `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify({ id: `e${seq}`, seq, type, turn_id: "t1", payload })}\n\n`;

function stream(frames: string[]) {
  return new ReadableStream({
    start(c) {
      for (const f of frames) c.enqueue(enc.encode(f));
      c.close();
    },
  });
}

describe("Session page", () => {
  it("renders snapshot, replays committed events by revision and shows availability separately", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1", { json: { session: session({ activity: "running", executor: { backend: "modal", lease_id: "l1", lease_state: "ready" }, worktree: { availability: "live", generation: 3, base_sha: "abc" } }), event_watermark: 4 } }],
      [
        "GET",
        "/api/sessions/s1/messages",
        {
          json: {
            event_watermark: 4,
            items: [
              { id: "m0", ordinal: 1, role: "user", content: [{ kind: "text", text: "Fix the build" }], turn_id: "t1", state: "accepted", parts: [] },
              { id: "m1", ordinal: 2, role: "assistant", content: [], turn_id: "t1", state: "streaming", parts: [{ key: "p1", kind: "text", revision: 2, content: "Looking", sealed: false }] },
            ],
          },
        },
      ],
      ["GET", "/api/sessions/s1/turns", { json: { items: [{ id: "t1", ordinal: 1, state: "running", reason: null, retry_of_turn_id: null, error: null, outcome: null, actions: ["cancel"], version: 1 }] } }],
      [
        "GET",
        "/api/sessions/s1/executor",
        { json: { backend: "modal", leases: [], worktree: { availability: "live", generation: 3, base_sha: "abc" }, recovery_point: { snapshot_id: "snap1", generation: 2, created_at: "2026-10-05T00:00:00Z" } } },
      ],
      [
        "GET",
        "/api/sessions/s1/events",
        (call) =>
          call.headers.Accept === "text/event-stream"
            ? {
                body: stream([
                  frame(4, "turn.started", {}), // already covered by the snapshot watermark
                  frame(5, "message.part_updated", { message_id: "m1", part_key: "p1", kind: "text", revision: 3, mode: "replace", content: "Looking at the failing check" }),
                  frame(6, "message.part_updated", { message_id: "m1", part_key: "p1", kind: "text", revision: 3, mode: "replace", content: "Looking at the failing check" }),
                ]),
                headers: { "content-type": "text/event-stream" },
              }
            : { json: { items: [], next_after: 4, event_watermark: 4 } },
      ],
    ]);
    await renderApp(
      <Routes>
        <Route path="/sessions/:id/:tab?" element={<SessionPage />} />
      </Routes>,
      m.fetch,
      "/sessions/s1",
    );

    expect(await screen.findByRole("heading", { name: "Fix the build" })).toBeInTheDocument();
    const log = await screen.findByRole("log", { name: "Conversation" });
    expect(log).toHaveAttribute("aria-live", "polite");
    expect(await within(log).findByText("Fix the build")).toBeInTheDocument();
    await waitFor(() => expect(within(log).getByText("Looking at the failing check")).toBeInTheDocument());
    expect(within(log).queryByText(/LookingLooking/)).toBeNull();

    // availability/recovery point are shown apart from the conversation activity
    const panel = screen.getByRole("region", { name: "Compute & recovery" });
    expect(panel).toHaveTextContent("Compute: ready");
    expect(panel).toHaveTextContent("Worktree: live");
    expect(screen.getByTestId("recovery-point")).toHaveTextContent("Generation 2");
    // One status, from the live Turn, in words; the Stop action names what it does.
    expect(screen.getByTestId("session-status")).toHaveTextContent("Working");
    const status = within(log).getByTestId("turn-status");
    expect(status).toHaveAttribute("data-state", "running");
    expect(status).toHaveTextContent("Working on your request");
    expect(within(status).getByRole("button", { name: "Stop" })).toBeInTheDocument();
    // The running Turn has reported no usage: nothing is invented, and no cost is guessed.
    const usage = screen.getByRole("region", { name: "Usage" });
    expect(usage).toHaveTextContent("The CLI has not reported token usage yet.");
    expect(document.body.innerHTML).not.toMatch(/20,604|\$0\.003|Effort: low/);
    expect(screen.getByRole("region", { name: "Details" })).toHaveTextContent("OpenCode");
    expect(m.calls.filter((c) => c.method === "POST")).toHaveLength(0);
    // The conversation stays put while workspace panels switch beside it.
    for (const tab of ["Overview", "Changes", "Files", "Terminal", "Services", "Child Sessions", "Activity"]) {
      expect(screen.getByRole("button", { name: tab })).toBeInTheDocument();
    }
    await userEvent.click(screen.getByRole("button", { name: "Activity" }));
    expect(screen.getByRole("log", { name: "Conversation" })).toBeInTheDocument();
    // The timeline speaks in words; per-chunk text updates are hidden until asked for.
    const timeline = await screen.findByRole("list", { name: "Events" });
    expect(timeline).not.toHaveTextContent("message.part_updated");
    await userEvent.click(screen.getByLabelText("Show streaming updates"));
    expect(within(screen.getByRole("list", { name: "Events" })).getAllByText("Reply text updated")).toHaveLength(2);
  });
});

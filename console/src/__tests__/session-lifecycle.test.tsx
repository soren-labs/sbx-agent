import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { ChangeSet, EventEnvelope, Session, Turn } from "../api/types";
import { startupStage } from "../features/sessions/ConversationPane";
import { SessionPage } from "../features/sessions/SessionPage";
import { SessionsPage } from "../features/sessions/SessionsPage";
import { ActivityTab, mergeEvents } from "../features/sessions/tabs/ActivityTab";
import { ChildrenTab } from "../features/sessions/tabs/ChildrenTab";
import { initialLiveState } from "../state/session-events";
import type { SessionLiveState } from "../state/session-live";
import { identityRoutes, mockFetch, renderApp, session, type RecordedCall } from "../test/helpers";

const turn = (over: Partial<Turn> = {}): Turn => ({
  id: "t1",
  ordinal: 1,
  state: "cancelled",
  reason: "cancelled_by_user",
  retry_of_turn_id: null,
  error: null,
  outcome: null,
  actions: [],
  version: 3,
  started_at: "2026-10-15T00:00:00Z",
  finished_at: "2026-10-15T00:00:09Z",
  ...over,
});

/** A Session page whose snapshot holds one user Message and one Turn. */
function sessionRoutes(s: Session, turns: Turn[], extra: Parameters<typeof mockFetch>[0] = []) {
  return mockFetch([
    ...identityRoutes,
    ["GET", `/api/sessions/${s.id}`, { json: { session: s, event_watermark: 4 } }],
    [
      "GET",
      `/api/sessions/${s.id}/messages`,
      {
        json: {
          event_watermark: 4,
          items: [{ id: "m0", ordinal: 1, role: "user", content: [{ kind: "text", text: "Do the thing" }], turn_id: "t1", state: "accepted", parts: [] }],
        },
      },
    ],
    ["GET", `/api/sessions/${s.id}/turns`, { json: { items: turns } }],
    ["GET", `/api/sessions/${s.id}/executor`, { json: { backend: "local", leases: [], worktree: null, recovery_point: null } }],
    ["GET", `/api/sessions/${s.id}/events`, (call: RecordedCall) => (call.headers.Accept === "text/event-stream" ? { status: 204 } : { json: { items: [], next_after: 4, event_watermark: 4 } })],
    ...extra,
  ]);
}

async function openSession(m: ReturnType<typeof mockFetch>, path = "/sessions/s1") {
  await renderApp(
    <Routes>
      <Route path="/sessions/:id/:tab?" element={<SessionPage />} />
    </Routes>,
    m.fetch,
    path,
  );
}

describe("Session lifecycle and actions (issue #228)", () => {
  it("AUD22: a closed Session never offers Retry, even if a stale Turn still lists it", async () => {
    const m = sessionRoutes(session({ lifecycle: "closed", actions: [] }), [turn({ actions: ["retry"] })]);
    await openSession(m);
    expect(await screen.findByText("Turn cancelled")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.getByText(/closed/i, { selector: ".conv-banner" })).toBeInTheDocument();
  });

  it("AUD22: an open Session offers the Retry the server lists", async () => {
    const m = sessionRoutes(session(), [turn({ actions: ["retry"] })]);
    await openSession(m);
    expect(await screen.findByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("AUD01: a cancelling Turn reads as stopping, never as a raw key or an outcome", async () => {
    const m = sessionRoutes(session({ activity: "running" }), [turn({ state: "cancelling", finished_at: null, reason: "cancelled_by_user" })]);
    await openSession(m);
    const bar = await screen.findByTestId("turn-status");
    expect(bar).toHaveTextContent("Stopping the turn");
    expect(bar).toHaveTextContent("waiting for the sandbox to confirm the stop");
    expect(document.body).not.toHaveTextContent("turn.cancelling");
    expect(screen.getByRole("banner")).toHaveTextContent("Stopping");
  });

  it("AUD13: the header names an archived Session as Archived, not Idle", async () => {
    const m = sessionRoutes(session({ lifecycle: "archived", activity: "idle", actions: ["unarchive", "close", "note"] }), [turn({ state: "succeeded" })]);
    await openSession(m);
    const header = await screen.findByRole("banner");
    await waitFor(() => expect(header).toHaveTextContent("Archived"));
    expect(header).not.toHaveTextContent("Idle");
  });

  it("AUD18: Close asks first, names the Session, explains the consequences and offers Archive", async () => {
    const s = session({ title: "Payments refactor" });
    const m = sessionRoutes(s, [turn({ state: "running", finished_at: null, actions: ["cancel"] })], [
      ["POST", "/api/sessions/s1/closures", { json: { session: { ...s, lifecycle: "closed", actions: [] } } }],
      ["POST", "/api/sessions/s1/archives", { json: { session: { ...s, lifecycle: "archived" } } }],
    ]);
    await openSession(m);
    await userEvent.click(await screen.findByRole("button", { name: "Session actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Close session…" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Close “Payments refactor” permanently?");
    expect(dialog).toHaveTextContent("Stops the 1 running or queued turn(s)");
    expect(dialog).toHaveTextContent("Releases its compute");
    expect(dialog).toHaveTextContent("read-only");
    expect(dialog).toHaveTextContent("Archived sessions can be reopened");
    // Nothing happened yet: Close is never one click.
    expect(m.find("POST", "/api/sessions/s1/closures")).toHaveLength(0);
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(m.find("POST", "/api/sessions/s1/closures")).toHaveLength(0);

    await userEvent.click(screen.getByRole("button", { name: "Session actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Close session…" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Archive instead" }));
    await waitFor(() => expect(m.find("POST", "/api/sessions/s1/archives")).toHaveLength(1));
    expect(m.find("POST", "/api/sessions/s1/closures")).toHaveLength(0);

    await userEvent.click(screen.getByRole("button", { name: "Session actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Close session…" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Close permanently" }));
    await waitFor(() => expect(m.find("POST", "/api/sessions/s1/closures")).toHaveLength(1));
    expect(m.find("POST", "/api/sessions/s1/closures")[0].headers["Idempotency-Key"]).toBeTruthy();
  });
});

describe("Startup stages (LEGACY198-A)", () => {
  const lease = (state: string, stage: string) => ({ id: "l1", generation: 1, state, reason: null, stage });
  const view = (...leases: ReturnType<typeof lease>[]) => ({ backend: "modal", leases, worktree: { availability: "live", generation: 1, base_sha: null }, recovery_point: null });

  it("names the stage the durable lease is in, never a guess from elapsed time", () => {
    const preparing = turn({ state: "preparing", finished_at: null });
    expect(startupStage(turn({ state: "queued" }), null)).toBe("turn.queued");
    expect(startupStage(turn({ state: "preparing", reason: "waiting_capacity" }), null)).toBe("turn.waiting_capacity");
    expect(startupStage(preparing, null)).toBe("turn.stage.requesting");
    expect(startupStage(preparing, view(lease("lost", "lost")))).toBe("turn.stage.requesting");
    expect(startupStage(preparing, view(lease("allocating", "allocating_sandbox")))).toBe("turn.stage.allocating_sandbox");
    expect(startupStage(preparing, view(lease("allocating", "connecting_runtime")))).toBe("turn.stage.connecting_runtime");
    expect(startupStage(preparing, view(lease("ready", "ready")))).toBe("turn.stage.starting_cli");
  });
});

describe("Children: explicit No ChangeSet (AUD10)", () => {
  const cs = (id: string): ChangeSet => ({
    id,
    session_id: "s1",
    source_turn_id: null,
    state: "ready",
    origin: "explicit",
    subject_digest: "sha256:x",
    repository: null,
    base_sha: "b",
    head_sha: "h",
    worktree_generation: 1,
    file_count: 1,
    error: null,
    created_at: "2026-10-15T00:00:00Z",
    sealed_at: "2026-10-15T00:00:01Z",
  });

  it("keeps No ChangeSet through refetches and role changes, and sends no subject", async () => {
    let sets: ChangeSet[] = [];
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/delegations", { json: { items: [] } }],
      ["GET", "/api/sessions/s1/changesets", () => ({ json: { items: sets } })],
      ["POST", "/api/sessions/s1/delegations", { status: 202, json: { delegation_id: "d1" } }],
    ]);
    sets = [cs("cs_aaaaaa")];
    // A new revision (an event arrived) refetches ChangeSets inside the same page.
    function Host() {
      const [revision, setRevision] = useState(0);
      return (
        <>
          <button type="button" onClick={() => setRevision((r) => r + 1)}>
            new event
          </button>
          <ChildrenTab sessionId="s1" revision={revision} />
        </>
      );
    }
    await renderApp(<Host />, m.fetch);
    const picker = await screen.findByLabelText("ChangeSet");
    await waitFor(() => expect(picker).toHaveValue("cs_aaaaaa")); // default once loaded
    await userEvent.selectOptions(picker, "");
    expect(picker).toHaveValue("");
    // A refetch brings another ready ChangeSet; the explicit choice is not overridden.
    sets = [cs("cs_bbbbbb"), cs("cs_aaaaaa")];
    await userEvent.click(screen.getByRole("button", { name: "new event" }));
    await waitFor(() => expect(within(picker).getAllByRole("option")).toHaveLength(3));
    expect(picker).toHaveValue("");
    await userEvent.selectOptions(screen.getByLabelText("Role"), "test");
    expect(picker).toHaveValue("");
    await userEvent.click(screen.getByRole("button", { name: /Spawn/ }));
    await waitFor(() => expect(m.find("POST", "/api/sessions/s1/delegations")).toHaveLength(1));
    expect(m.find("POST", "/api/sessions/s1/delegations")[0].body).toEqual({ role: "test" });
  });
});

describe("Activity history (AUD12)", () => {
  const ev = (seq: number, type = "turn.succeeded"): EventEnvelope => ({ id: `e${seq}`, seq, type, payload: {}, recorded_at: "2026-10-15T00:00:00Z" });

  it("merges history pages with the live stream by seq, newest first, without duplicates", () => {
    expect(mergeEvents([ev(3), ev(2)], [ev(3), ev(5)], [ev(1)]).map((e) => e.seq)).toEqual([5, 3, 2, 1]);
  });

  it("loads a bounded page of committed events after a reload and pages further back", async () => {
    const m = mockFetch([
      ...identityRoutes,
      [
        "GET",
        "/api/sessions/s1/events",
        (call) => {
          const before = Number(call.query.get("before"));
          return before === 41
            ? { json: { items: [ev(39, "session.archived"), ev(40, "turn.succeeded")], next_before: 39, event_watermark: 40 } }
            : { json: { items: [ev(38, "session.created")], next_before: null, event_watermark: 40 } };
        },
      ],
    ]);
    const state: SessionLiveState = { ...(initialLiveState() as unknown as SessionLiveState), watermark: 40, lastSeq: 41, events: [ev(40), ev(41, "turn.queued")], turns: [] } as SessionLiveState;
    await renderApp(<ActivityTab sessionId="s1" state={state} />, m.fetch);
    const timeline = await screen.findByRole("list", { name: "Events" });
    await waitFor(() => expect(within(timeline).getAllByRole("listitem")).toHaveLength(3)); // 41 live + 40, 39 history
    expect(m.find("GET", "/api/sessions/s1/events")[0].query.get("before")).toBe("41");
    expect(m.find("GET", "/api/sessions/s1/events")[0].query.get("limit")).toBe("100");
    await userEvent.click(screen.getByRole("button", { name: "Load earlier events" }));
    await waitFor(() => expect(within(timeline).getAllByRole("listitem")).toHaveLength(4));
    expect(screen.queryByRole("button", { name: "Load earlier events" })).toBeNull();
    expect(screen.queryByText("No events yet.")).toBeNull();
  });
});

describe("All sessions page (AUD13/AUD14)", () => {
  it("lists open Sessions by default, loads older pages without duplicates and searches the server", async () => {
    const mk = (i: number) => session({ id: `s${i}`, title: `Session ${i}`, updated_at: `2026-10-15T00:${String(59 - i).padStart(2, "0")}:00Z` });
    const m = mockFetch([
      ...identityRoutes,
      [
        "GET",
        "/api/workspaces/w1/sessions",
        (call) => {
          if (call.query.get("q")) return { json: { items: [mk(53)], next_cursor: null } };
          // The second page overlaps the first by one (a Session moved while paging).
          return call.query.get("cursor")
            ? { json: { items: [mk(49), ...Array.from({ length: 4 }, (_, i) => mk(50 + i))], next_cursor: null } }
            : { json: { items: Array.from({ length: 50 }, (_, i) => mk(i)), next_cursor: "c1" } };
        },
      ],
    ]);
    await renderApp(
      <Routes>
        <Route path="/sessions" element={<SessionsPage />} />
      </Routes>,
      m.fetch,
      "/sessions",
    );
    const table = await screen.findByRole("table", { name: "Sessions" });
    await waitFor(() => expect(within(table).getAllByRole("link")).toHaveLength(50));
    expect(m.find("GET", "/api/workspaces/w1/sessions")[0].query.get("lifecycle")).toBe("open");
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(within(table).getAllByRole("link")).toHaveLength(54));
    expect(within(table).getAllByRole("link", { name: /Session 49\b/ })).toHaveLength(1);
    expect(within(table).getByRole("link", { name: /Session 53/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();

    await userEvent.type(screen.getByLabelText("Search sessions"), "Session 53");
    await waitFor(() => expect(m.find("GET", "/api/workspaces/w1/sessions").some((c) => c.query.get("q") === "Session 53")).toBe(true));
    await waitFor(() => expect(within(screen.getByRole("table", { name: "Sessions" })).getAllByRole("link")).toHaveLength(1));
    expect(screen.getByText(/Search covers every session/)).toBeInTheDocument();
    await act(async () => undefined);
  });
});

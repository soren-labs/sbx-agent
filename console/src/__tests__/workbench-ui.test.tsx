import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { Message, Session, Turn } from "../api/types";
import { SessionPage } from "../features/sessions/SessionPage";
import { Popover } from "../ui/Popover";
import { identityRoutes, mockFetch, renderApp, session } from "../test/helpers";

const enc = new TextEncoder();
const frame = (seq: number, type: string, payload: Record<string, unknown>) =>
  `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify({ id: `e${seq}`, seq, type, turn_id: "t1", payload })}\n\n`;

const user: Message = {
  id: "m0",
  ordinal: 1,
  role: "user",
  content: [{ kind: "text", text: "Fix the build" }],
  turn_id: "t1",
  state: "accepted",
  parts: [],
};

const runningTurn = (over: Partial<Turn> = {}): Turn => ({
  id: "t1",
  ordinal: 1,
  state: "running",
  reason: null,
  retry_of_turn_id: null,
  error: null,
  outcome: null,
  actions: ["cancel"],
  version: 1,
  created_at: "2026-10-05T00:00:00Z",
  started_at: "2026-10-05T00:00:02Z",
  ...over,
});

function mount(opts: { messages?: Message[]; turns?: Turn[]; activity?: string; stream?: ReadableStream; sessionOver?: Partial<Session>; path?: string }) {
  const body =
    opts.stream ??
    new ReadableStream({
      start(c) {
        c.close();
      },
    });
  const m = mockFetch([
    ...identityRoutes,
    [
      "GET",
      "/api/sessions/s1",
      {
        json: {
          session: session({ activity: opts.activity ?? "running", harness: { provider_id: "claude", model: "flash" }, ...opts.sessionOver }),
          event_watermark: 4,
        },
      },
    ],
    ["GET", "/api/sessions/s1/messages", { json: { event_watermark: 4, items: opts.messages ?? [user] } }],
    ["GET", "/api/sessions/s1/turns", { json: { items: opts.turns ?? [runningTurn()] } }],
    ["GET", "/api/sessions/s1/executor", { json: { backend: "modal", leases: [], worktree: { availability: "live", generation: 1, base_sha: null }, recovery_point: null } }],
    [
      "GET",
      "/api/sessions/s1/events",
      (call) =>
        call.headers.Accept === "text/event-stream"
          ? { body, headers: { "content-type": "text/event-stream" } }
          : { json: { items: [], next_after: 4, event_watermark: 4 } },
    ],
    ["POST", "/api/sessions/s1/messages", { status: 202, json: { message_id: "m9" } }],
    ["POST", "/api/turns/t1/cancellations", { status: 202, json: {} }],
  ]);
  return {
    m,
    ready: renderApp(
      <Routes>
        <Route path="/sessions/:id/:tab?" element={<SessionPage />} />
      </Routes>,
      m.fetch,
      opts.path ?? "/sessions/s1",
    ),
  };
}

describe("anchored popovers", () => {
  it("flips to the roomier side and clamps max-height to the space there", async () => {
    // Trigger near the top edge: 40px above, ~520px below, viewport 600px tall.
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const r = { x: 20, y: 40, left: 20, top: 40, width: 40, height: 30, right: 60, bottom: 70 } as DOMRect;
      return this.dataset?.anchor === undefined ? r : r;
    });
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(400);
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(360);
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 600 });
    render(
      <Popover open side="top" width={360} trigger={<button type="button">open</button>}>
        <div>panel content</div>
      </Popover>,
    );
    const panel = screen.getByText("panel content").closest(".popover__panel") as HTMLElement;
    await waitFor(() => expect(panel.dataset.side).toBe("bottom"));
    expect(panel.style.maxHeight).toBe("516px"); // 600 − 70 − 6 gap − 8 viewport pad
    expect(panel.style.overflowY).toBe("auto");
    vi.restoreAllMocks();
  });

  it("keeps the requested side when the panel fits", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      () => ({ x: 20, y: 40, left: 20, top: 40, width: 40, height: 30, right: 60, bottom: 70 }) as DOMRect,
    );
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(200);
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(360);
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 600 });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    render(
      <Popover open side="bottom" width={360} trigger={<button type="button">open</button>}>
        <div>panel</div>
      </Popover>,
    );
    const panel = screen.getByText("panel").closest(".popover__panel") as HTMLElement;
    await waitFor(() => expect(panel.dataset.side).toBe("bottom"));
    // 360px requested on a 390px viewport: fits within 100vw − 16px, stays left-clamped.
    expect(Number.parseFloat(panel.style.width)).toBe(360);
    expect(panel.style.maxHeight).toBe("516px");
    vi.restoreAllMocks();
  });
});

describe("workbench UI behaviours", () => {
  it("turns the send button into Stop while a Turn is live and the box is empty", async () => {
    const { ready } = mount({});
    await ready;
    const composer = screen.getByRole("form", { name: "Send a message" });
    // The cancel affordance in the composer appears only while a Turn is live and nothing is typed.
    expect(within(composer).getByRole("button", { name: "Stop" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Message"), "draft");
    expect(within(composer).queryByRole("button", { name: "Stop" })).toBeNull();
    expect(within(composer).getByRole("button", { name: "Send" })).toBeInTheDocument();
  });

  it("offers Jump to latest when output arrives while the reader is scrolled up", async () => {
    let push: (f: string) => void = () => {};
    const body = new ReadableStream({
      start(c) {
        push = (f) => c.enqueue(enc.encode(f));
      },
    });
    const { ready } = mount({ messages: [user], stream: body });
    await ready;
    const log = await screen.findByRole("log", { name: "Conversation" });
    Object.defineProperties(log, {
      scrollHeight: { configurable: true, value: 2000 },
      clientHeight: { configurable: true, value: 500 },
    });
    log.scrollTop = 100;
    fireEvent.scroll(log);
    await act(async () => {
      push(frame(5, "message.part_added", { message_id: "m9", part_key: "p1", kind: "text", revision: 1, mode: "append", content: "new output" }));
    });
    const jump = await screen.findByRole("button", { name: "Jump to latest" });
    await userEvent.click(jump);
    expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();
  });

  it("keeps the draft when the pane switches between conversation and workspace", async () => {
    const { ready } = mount({});
    await ready;
    const box = (await screen.findByLabelText("Message")) as HTMLTextAreaElement;
    await userEvent.type(box, "keep me");
    await userEvent.click(screen.getByRole("tab", { name: "Overview" }));
    expect(await screen.findByRole("region", { name: "Compute & recovery" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Conversation" }));
    expect(((await screen.findByLabelText("Message")) as HTMLTextAreaElement).value).toBe("keep me");
  });

  it("explains terminal Turn reasons in words while keeping the raw code", async () => {
    const { ready } = mount({ turns: [runningTurn({ state: "cancelled", reason: "cancelled_by_user", actions: [] })] });
    await ready;
    const outcome = await screen.findByTestId("turn-status");
    expect(outcome).toHaveTextContent("Cancelled by you.");
    const code = within(outcome).getByText("cancelled_by_user");
    expect(code.tagName).toBe("CODE");
    expect(outcome).toHaveAttribute("data-state", "cancelled");
  });

  it("collapses inactive workspace tabs to icons only when the labelled row overflows", async () => {
    let roCb: (entries: { contentRect: { width: number } }[], ro: unknown) => void = () => {};
    class FakeRO {
      constructor(cb: (entries: { contentRect: { width: number } }[], ro: unknown) => void) {
        roCb = cb;
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", FakeRO);
    const { ready } = mount({ path: "/sessions/s1/overview" });
    await ready;
    const tabs = document.querySelector(".wpanel__tabs") as HTMLElement;
    expect(tabs).toBeTruthy();
    // Labelled row wants 720px but the panel only has 500px.
    Object.defineProperty(tabs, "scrollWidth", { configurable: true, value: 720 });
    Object.defineProperty(tabs, "clientWidth", { configurable: true, value: 500 });
    act(() => roCb([{ contentRect: { width: 500 } }], null));
    await waitFor(() => expect(tabs.className).toContain("wpanel__tabs--icons"));
    // All tabs keep their accessible names; only the active one shows its label (CSS).
    for (const name of ["Overview", "Changes", "Files", "Terminal", "Services", "Children", "Activity"]) {
      expect(within(tabs).getByRole("tab", { name })).toHaveAttribute("title", name);
    }
    expect(tabs.querySelector(".wpanel__tab--active .wpanel__tab-label")).toHaveTextContent("Overview");
    // Widening back past the natural width restores full labels.
    act(() => roCb([{ contentRect: { width: 800 } }], null));
    await waitFor(() => expect(tabs.className).not.toContain("wpanel__tabs--icons"));
  });

  it("shows no compute actions on a closed Session and keys them to the lease otherwise", async () => {
    const closed = mount({ sessionOver: { lifecycle: "closed" }, path: "/sessions/s1/overview" });
    await closed.ready;
    expect(await screen.findByTestId("compute-closed")).toHaveTextContent("Compute is released when a session closes");
    expect(screen.queryByRole("button", { name: "Activate compute" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Release compute" })).toBeNull();
  });

  it("offers only Release while a lease is live and only Activate without one", async () => {
    const live = mount({
      sessionOver: { executor: { backend: "modal", lease_id: "l1", lease_state: "ready" } },
      path: "/sessions/s1/overview",
    });
    await live.ready;
    await screen.findByRole("button", { name: "Release compute" });
    expect(screen.queryByRole("button", { name: "Activate compute" })).toBeNull();
  });

  it("explains released compute on a closed Session instead of offering Start terminal", async () => {
    const { ready } = mount({ sessionOver: { lifecycle: "closed" }, path: "/sessions/s1/terminal" });
    await ready;
    expect(await screen.findByTestId("terminal-closed")).toHaveTextContent("Compute is released when a session closes");
    expect(screen.queryByRole("button", { name: "Start terminal" })).toBeNull();
    expect(screen.queryByRole("button", { name: "New terminal" })).toBeNull();
  });
});

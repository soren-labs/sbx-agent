import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { Message, Turn } from "../api/types";
import { SessionPage } from "../features/sessions/SessionPage";
import { identityRoutes, mockFetch, renderApp, session } from "../test/helpers";

const enc = new TextEncoder();
const frame = (seq: number, type: string, payload: Record<string, unknown>) =>
  `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify({ id: `e${seq}`, seq, type, turn_id: "t1", payload })}\n\n`;
const sse = (frames: string[]) =>
  new ReadableStream({
    start(c) {
      for (const f of frames) c.enqueue(enc.encode(f));
      c.close();
    },
  });

const turn = (over: Partial<Turn> = {}): Turn => ({
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
const user: Message = { id: "m0", ordinal: 1, role: "user", content: [{ kind: "text", text: "Fix the build" }], turn_id: "t1", state: "accepted", parts: [] };
const reply = (parts: Message["parts"], state = "streaming"): Message => ({
  id: "m1",
  ordinal: 2,
  role: "assistant",
  content: [],
  turn_id: "t1",
  state,
  parts,
});
const toolPart = (id: string, name: string, status: string, input: unknown, output?: string) => ({
  key: `tool:${id}`,
  kind: "tool",
  revision: 1,
  content: "",
  data: { tool_id: id, name, status, input, ...(output === undefined ? {} : { output }) },
});

function mount(opts: { messages: Message[]; turns: Turn[]; frames?: string[]; activity?: string; harness?: string }) {
  const m = mockFetch([
    ...identityRoutes,
    ["GET", "/api/sessions/s1", { json: { session: session({ activity: opts.activity ?? "running", harness: { provider_id: opts.harness ?? "claude", model: "flash" } }), event_watermark: 4 } }],
    ["GET", "/api/sessions/s1/messages", { json: { event_watermark: 4, items: opts.messages } }],
    ["GET", "/api/sessions/s1/turns", { json: { items: opts.turns } }],
    ["GET", "/api/sessions/s1/executor", { json: { backend: "modal", leases: [], worktree: { availability: "live", generation: 1, base_sha: null }, recovery_point: null } }],
    [
      "GET",
      "/api/sessions/s1/events",
      (call) =>
        call.headers.Accept === "text/event-stream"
          ? { body: sse(opts.frames ?? []), headers: { "content-type": "text/event-stream" } }
          : { json: { items: [], next_after: 4, event_watermark: 4 } },
    ],
    ["POST", "/api/sessions/s1/messages", { status: 202, json: { message_id: "m9" } }],
    ["POST", "/api/turns/t1/retries", { status: 202, json: { turn_id: "t2" } }],
    ["POST", "/api/turns/t1/cancellations", { status: 202, json: {} }],
  ]);
  return {
    m,
    ready: renderApp(
      <Routes>
        <Route path="/sessions/:id/:tab?" element={<SessionPage />} />
      </Routes>,
      m.fetch,
      "/sessions/s1",
    ),
  };
}

describe("Session workbench", () => {
  it("groups live tool events into one running work block and updates steps in place", async () => {
    const { ready } = mount({
      messages: [user, reply([{ key: "p1", kind: "text", revision: 1, content: "I'll run the tests." }])],
      turns: [turn()],
      frames: [
        frame(5, "tool.started", { message_id: "m1", tool_id: "c1", name: "Bash", status: "running", input: { command: "python -m pytest -q" } }),
        frame(6, "tool.completed", { message_id: "m1", tool_id: "c1", name: "Bash", status: "completed", input: { command: "python -m pytest -q" }, output: "8 passed in 0.03s" }),
        frame(7, "tool.started", { message_id: "m1", tool_id: "c2", name: "Write", status: "running", input: { file_path: "/work/README.md", content: "# Calc\nUsage\n" } }),
      ],
    });
    await ready;
    const log = await screen.findByRole("log", { name: "Conversation" });
    const group = await within(log).findByTestId("work-group");
    await waitFor(() => expect(within(group).getAllByRole("listitem")).toHaveLength(2));
    // The agent is named by its real CLI; the block says what is happening in words.
    expect(within(log).getByText("Claude Code")).toBeInTheDocument();
    expect(group).toHaveTextContent("Working");
    expect(group).toHaveTextContent("1 command · 1 file edit");
    const [ran, writing] = within(group).getAllByRole("listitem");
    expect(ran).toHaveTextContent("Ran");
    expect(ran).toHaveTextContent("python -m pytest -q");
    expect(within(writing).getByRole("img", { name: "Running" })).toBeInTheDocument();
    expect(log).not.toHaveTextContent("tool.started");
    // Raw input/output is one click away and shown as text, not escaped JSON.
    await userEvent.click(within(ran).getByRole("button"));
    expect(ran).toHaveTextContent("8 passed in 0.03s");
    await userEvent.click(within(writing).getByRole("button"));
    expect(within(writing).getByText(/# Calc/).textContent).toBe("content:\n# Calc\nUsage");
    // A follow-up typed now is queued behind the running Turn; Enter sends, Shift+Enter does not.
    const box = screen.getByLabelText("Message");
    expect(box).toHaveAttribute("placeholder", "Queue a follow-up for when this turn finishes…");
  });

  it("collapses finished work, reports real duration and usage, and never a cost", async () => {
    const { ready } = mount({
      activity: "awaiting_input",
      messages: [
        user,
        reply(
          [
            toolPart("c1", "Bash", "completed", { command: "ls" }, Array.from({ length: 40 }, (_, i) => `file-${i}`).join("\n")),
            toolPart("c2", "Bash", "error", { command: "false" }, "exit 1"),
            { key: "p1", kind: "text", revision: 1, content: "| File | Contents |\n| --- | --- |\n| `a.py` | code |\n\n```py\nprint(1)\n```" },
          ],
          "completed",
        ),
      ],
      turns: [turn({ state: "succeeded", actions: [], finished_at: "2026-10-05T00:01:14Z", usage: { input_tokens: 1200, cached_input_tokens: 19400, output_tokens: 310 } })],
    });
    await ready;
    const log = await screen.findByRole("log", { name: "Conversation" });
    const group = await within(log).findByTestId("work-group");
    expect(group).toHaveTextContent("Worked");
    expect(group).toHaveTextContent("2 commands");
    expect(group).toHaveTextContent("1 failed");
    expect(within(group).queryAllByRole("listitem")).toHaveLength(0);
    // GFM renders as a table and a code block.
    expect(within(log).getByRole("table")).toBeInTheDocument();
    expect(within(log).getByText("print(1)")).toBeInTheDocument();
    const status = within(log).getByTestId("turn-status");
    expect(status).toHaveTextContent("Completed in 1m 12s");
    expect(status).toHaveTextContent("20.6k tokens in · 310 out");
    expect(document.body.textContent).not.toMatch(/\$\d/);
    expect(screen.getByTestId("session-status")).toHaveTextContent("Waiting for you");
    // Long output is bounded with an exact count of what is hidden, then fully available.
    await userEvent.click(within(group).getByRole("button", { name: /Worked/ }));
    const first = within(group).getAllByRole("listitem")[0];
    await userEvent.click(within(first).getByRole("button"));
    expect(first).not.toHaveTextContent("file-39");
    await userEvent.click(within(first).getByRole("button", { name: "Show 26 more lines" }));
    expect(first).toHaveTextContent("file-39");
  });

  it("explains a failed Turn in plain language and retries through the API", async () => {
    const { m, ready } = mount({
      activity: "attention",
      messages: [user],
      turns: [
        turn({
          state: "failed",
          actions: ["retry"],
          finished_at: "2026-10-05T00:00:09Z",
          error: { code: "credential_invalid", message: "provider rejected the credential" },
        }),
      ],
    });
    await ready;
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This turn failed");
    expect(alert).toHaveTextContent("The model provider rejected the API key. Replace it in Connections, then retry.");
    expect(alert).toHaveTextContent("credential_invalid");
    expect(screen.getByTestId("session-status")).toHaveTextContent("Needs attention");
    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(m.find("POST", "/api/turns/t1/retries")).toHaveLength(1));
    expect(m.find("POST", "/api/turns/t1/retries")[0].headers["Idempotency-Key"]).toBeTruthy();
  });

  it("shows queued and starting Turns before any agent output exists", async () => {
    const { ready } = mount({ messages: [user], turns: [turn({ state: "preparing", started_at: null })], activity: "queued" });
    await ready;
    const status = await screen.findByTestId("turn-status");
    expect(status).toHaveAttribute("data-state", "preparing");
    expect(status).toHaveTextContent("Starting the sandbox and coding CLI");
    expect(screen.getByTestId("session-status")).toHaveTextContent("Starting");
  });

  it("sends with Enter, keeps Shift+Enter for new lines and clears only after acceptance", async () => {
    const { m, ready } = mount({ activity: "awaiting_input", messages: [user], turns: [turn({ state: "succeeded", actions: [] })] });
    await ready;
    const box = (await screen.findByLabelText("Message")) as HTMLTextAreaElement;
    await userEvent.type(box, "line one{Shift>}{Enter}{/Shift}line two");
    expect(box.value).toBe("line one\nline two");
    expect(m.find("POST", "/api/sessions/s1/messages")).toHaveLength(0);
    await userEvent.type(box, "{Enter}");
    await waitFor(() => expect(m.find("POST", "/api/sessions/s1/messages")).toHaveLength(1));
    expect(m.find("POST", "/api/sessions/s1/messages")[0].body).toEqual({ content: "line one\nline two", routing: "queue" });
    await waitFor(() => expect(box.value).toBe(""));
    await userEvent.click(screen.getByLabelText("Note only"));
    await userEvent.type(box, "fyi{Enter}");
    await waitFor(() => expect(m.find("POST", "/api/sessions/s1/messages")).toHaveLength(2));
    expect(m.find("POST", "/api/sessions/s1/messages")[1].body).toEqual({ content: "fyi", routing: "note" });
  });

  it("does not pull a reader back to the bottom; offers a jump instead", async () => {
    const { ready } = mount({ messages: [user, reply([{ key: "p1", kind: "text", revision: 1, content: "one" }])], turns: [turn()] });
    await ready;
    const log = await screen.findByRole("log", { name: "Conversation" });
    // The reader scrolled up into history.
    Object.defineProperties(log, {
      scrollHeight: { configurable: true, value: 2000 },
      clientHeight: { configurable: true, value: 500 },
    });
    log.scrollTop = 300;
    fireEvent.scroll(log);
    expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();
    await act(async () => {
      await userEvent.type(screen.getByLabelText("Message"), "x");
    });
    expect(log.scrollTop).toBe(300);
  });
});

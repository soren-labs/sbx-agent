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

function mount(opts: { messages: Message[]; turns: Turn[]; frames?: string[]; body?: ReadableStream; activity?: string; harness?: string; send?: () => Promise<{ status: number; json: unknown }> }) {
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
          ? { body: opts.body ?? sse(opts.frames ?? []), headers: { "content-type": "text/event-stream" } }
          : { json: { items: [], next_after: 4, event_watermark: 4 } },
    ],
    ["POST", "/api/sessions/s1/messages", opts.send ?? { status: 202, json: { message_id: "m9" } }],
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

  it("folds a long run of tool calls into readable rows and keeps every detail one click away", async () => {
    const done = (id: string, name: string, input: unknown, output = "ok") => toolPart(id, name, "completed", input, output);
    const parts = [
      ...Array.from({ length: 9 }, (_, i) => done(`r${i}`, "Read", { file_path: `/work/src/mod${i}.py` }, `# module ${i}`)),
      ...Array.from({ length: 4 }, (_, i) => done(`g${i}`, "Grep", { pattern: `needle${i}` }, `mod${i}.py:1`)),
      done("b1", "Bash", { command: "python -m pytest -q" }, "12 passed"),
      ...["a.py", "b.py", "c.py"].map((f, i) => done(`w${i}`, "Write", { file_path: `/work/src/${f}`, content: `print(${i})` })),
      { ...toolPart("e1", "Edit", "error", { file_path: "/work/src/a.py" }, "String to replace not found"), data: { tool_id: "e1", name: "Edit", status: "error", error: true, input: { file_path: "/work/src/a.py" }, output: "String to replace not found" } },
      ...Array.from({ length: 5 }, (_, i) => done(`r2${i}`, "Read", { file_path: `/work/tests/t${i}.py` })),
    ].map((part, i) => ({ ...part, created_at: `2026-10-05T00:00:${String(10 + i).padStart(2, "0")}Z`, updated_at: `2026-10-05T00:00:${String(11 + i).padStart(2, "0")}Z` }));
    expect(parts.length).toBeGreaterThanOrEqual(20);
    const { ready } = mount({
      activity: "awaiting_input",
      messages: [user, reply([...parts, { key: "p9", kind: "text", revision: 1, content: "All done." }], "completed")],
      turns: [turn({ state: "succeeded", actions: [], finished_at: "2026-10-05T00:01:00Z" })],
    });
    await ready;
    const group = await screen.findByTestId("work-group");
    // Finished work is one quiet line with its real duration, counts and the failure.
    expect(group).toHaveTextContent("Worked for 23s");
    expect(group).toHaveTextContent("1 command · 4 file edits · 14 reads · 4 searches");
    expect(group).toHaveTextContent("1 failed");
    expect(within(group).queryAllByRole("listitem")).toHaveLength(0);
    await userEvent.click(within(group).getByRole("button", { name: /Worked for/ }));
    // 23 tool calls read as 6 rows: folds for the quiet kinds, single rows for the rest.
    const rows = within(group).getAllByRole("listitem");
    expect(rows.map((r) => r.textContent?.replace(/\s+/g, " ").trim().slice(0, 40))).toEqual([
      expect.stringContaining("Read 9 files"),
      expect.stringContaining("Ran 4 searches"),
      expect.stringContaining("Ran" + "python -m pytest -q"),
      expect.stringContaining("Edited 3 files"),
      expect.stringContaining("Edited" + "/work/src/a.py"),
      expect.stringContaining("Read 5 files"),
    ]);
    expect(rows[0]).toHaveTextContent("mod0.py, mod1.py");
    expect(rows[4]).toHaveTextContent("Failed");
    // A fold opens to every step with its exact path; a step opens to its real output.
    await userEvent.click(within(rows[3]).getByRole("button"));
    const edits = within(rows[3]).getAllByTestId("work-step");
    expect(edits.map((e) => e.textContent)).toEqual([
      expect.stringContaining("/work/src/a.py"),
      expect.stringContaining("/work/src/b.py"),
      expect.stringContaining("/work/src/c.py"),
    ]);
    await userEvent.click(within(edits[1]).getByRole("button"));
    expect(edits[1]).toHaveTextContent("content: print(1)");
    await userEvent.click(within(rows[4]).getByRole("button"));
    expect(rows[4]).toHaveTextContent("String to replace not found");
  });

  it("keeps what the reader opened while new output keeps arriving", async () => {
    let push: (f: string) => void = () => {};
    const body = new ReadableStream({
      start(c) {
        push = (f) => c.enqueue(enc.encode(f));
      },
    });
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1", { json: { session: session({ activity: "running", harness: { provider_id: "claude", model: "flash" } }), event_watermark: 4 } }],
      ["GET", "/api/sessions/s1/messages", { json: { event_watermark: 4, items: [user, reply([toolPart("c1", "Bash", "completed", { command: "ls" }, "a.py"), { key: "p1", kind: "text", revision: 1, content: "Listed." }])] } }],
      ["GET", "/api/sessions/s1/turns", { json: { items: [turn()] } }],
      ["GET", "/api/sessions/s1/executor", { json: { backend: "modal", leases: [], worktree: { availability: "live", generation: 1, base_sha: null }, recovery_point: null } }],
      ["GET", "/api/sessions/s1/events", (call) => (call.headers.Accept === "text/event-stream" ? { body, headers: { "content-type": "text/event-stream" } } : { json: { items: [], next_after: 4, event_watermark: 4 } })],
    ]);
    await renderApp(
      <Routes>
        <Route path="/sessions/:id/:tab?" element={<SessionPage />} />
      </Routes>,
      m.fetch,
      "/sessions/s1",
    );
    const log = await screen.findByRole("log", { name: "Conversation" });
    const first = (await within(log).findAllByTestId("work-group"))[0];
    // Finished and followed by prose: collapsed. The reader opens it and one step.
    expect(within(first).queryAllByRole("listitem")).toHaveLength(0);
    await userEvent.click(within(first).getByRole("button", { name: /Worked/ }));
    await userEvent.click(within(within(first).getByRole("listitem")).getByRole("button"));
    expect(first).toHaveTextContent("a.py");
    // The reply keeps streaming, then another tool starts: the sentence between the two
    // tool calls becomes a line of the same work group instead of splitting it.
    await act(async () => {
      push(frame(5, "message.part_updated", { message_id: "m1", part_key: "p1", kind: "text", revision: 2, mode: "append", content: " Now the tests." }));
    });
    expect(await within(log).findByText("Listed. Now the tests.")).toBeInTheDocument();
    await act(async () => {
      push(frame(6, "tool.started", { message_id: "m1", tool_id: "c2", name: "Bash", status: "running", input: { command: "pytest -q" } }));
    });
    await waitFor(() => expect(within(log).getByTestId("work-note")).toHaveTextContent("Listed. Now the tests."));
    const groups = within(log).getAllByTestId("work-group");
    expect(groups).toHaveLength(1);
    expect(groups[0]).toHaveTextContent("2 commands");
    // Their choices survived: the group and the step they opened are still open.
    expect(groups[0]).toHaveTextContent("a.py");
    // The bar above the composer names the command that is actually running.
    const status = screen.getByTestId("turn-status");
    expect(status).toHaveTextContent("Running");
    expect(status).toHaveTextContent("pytest -q");
  });

  it("stops sliding live rows out of view once the reader has opened one", async () => {
    const cmd = (i: number, status = "completed") => toolPart(`c${i}`, "Bash", status, { command: `make step-${i}` }, `out ${i}`);
    let push: (f: string) => void = () => {};
    const body = new ReadableStream({
      start(c) {
        push = (f) => c.enqueue(enc.encode(f));
      },
    });
    const { ready } = mount({ messages: [user, reply(Array.from({ length: 10 }, (_, i) => cmd(i)))], turns: [turn()], body });
    await ready;
    const group = await screen.findByTestId("work-group");
    // Live: only the newest rows, with the exact number of earlier ones on offer.
    expect(within(group).getAllByTestId("work-step")).toHaveLength(8);
    expect(group).toHaveTextContent("Show 2 earlier steps");
    expect(group).not.toHaveTextContent("make step-1");
    await userEvent.click(within(within(group).getAllByTestId("work-step")[0]).getByRole("button"));
    expect(group).toHaveTextContent("out 2");
    for (const i of [10, 11, 12]) {
      await act(async () => {
        push(frame(i - 5, "tool.started", { message_id: "m1", tool_id: `c${i}`, name: "Bash", status: "running", input: { command: `make step-${i}` } }));
      });
    }
    await waitFor(() => expect(group).toHaveTextContent("make step-12"));
    // The row they opened is still there, still open, and nothing above it moved.
    expect(group).toHaveTextContent("out 2");
    expect(group).toHaveTextContent("Show 2 earlier steps");
    expect(within(group).getAllByTestId("work-step")).toHaveLength(11);
    await userEvent.click(within(group).getByRole("button", { name: "Show 2 earlier steps" }));
    expect(within(group).getAllByTestId("work-step")).toHaveLength(13);
  });

  it("shows a sent message at once and gives the words back if the server refuses it", async () => {
    let release: (r: { status: number; json: unknown }) => void = () => {};
    const { ready } = mount({
      activity: "awaiting_input",
      messages: [user],
      turns: [turn({ state: "succeeded", actions: [] })],
      send: () => new Promise((resolve) => (release = resolve)),
    });
    await ready;
    const box = (await screen.findByLabelText("Message")) as HTMLTextAreaElement;
    await userEvent.type(box, "Add a changelog{Enter}");
    // Before the server answered: the bubble is in the log and the box is free again.
    const bubble = await screen.findByTestId("outgoing");
    expect(bubble).toHaveTextContent("Add a changelog");
    expect(bubble).toHaveTextContent("sending");
    expect(box.value).toBe("");
    await act(async () => release({ status: 422, json: { error: { code: "validation_failed", message: "not accepted" } } }));
    await waitFor(() => expect(screen.queryByTestId("outgoing")).toBeNull());
    expect(box.value).toBe("Add a changelog");
    expect(screen.getByRole("button", { name: "Retry sending message" })).toBeInTheDocument();
  });
});

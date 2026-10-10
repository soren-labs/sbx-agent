import { describe, expect, it } from "vitest";
import type { Message, MessagePart, Turn } from "../api/types";
import {
  activityOf,
  blocksOf,
  countSteps,
  entriesOf,
  isStreaming,
  namesOf,
  rowsOf,
  spanOf,
  formatDuration,
  formatTokens,
  inputText,
  previewOf,
  secondsBetween,
  stepKind,
  toStep,
} from "../features/sessions/worklog";

const tool = (key: string, name: string, status: string, input: unknown = {}, extra: Record<string, unknown> = {}): MessagePart => ({
  key: `tool:${key}`,
  kind: "tool",
  revision: 1,
  content: "",
  data: { tool_id: key, name, status, input, ...extra },
});
const text = (key: string, content: string): MessagePart => ({ key, kind: "text", revision: 1, content });
const message = (parts: MessagePart[], over: Partial<Message> = {}): Message => ({
  id: "m1",
  ordinal: 2,
  role: "assistant",
  content: [],
  turn_id: "t1",
  state: "streaming",
  parts,
  ...over,
});
const turn = (over: Partial<Turn> = {}): Turn => ({
  id: "t1",
  ordinal: 1,
  state: "running",
  reason: null,
  retry_of_turn_id: null,
  error: null,
  outcome: null,
  actions: [],
  version: 1,
  ...over,
});

describe("worklog model", () => {
  it("classifies the tool names of all five official CLIs", () => {
    // OpenCode, Codex, Claude Code, Grok Build, Command Code
    for (const name of ["bash", "shell", "Bash", "run_terminal_command", "shell_command"]) expect(stepKind(name)).toBe("command");
    for (const name of ["write", "file_change", "Write", "Edit", "search_replace", "write_file"]) expect(stepKind(name)).toBe("edit");
    for (const name of ["read", "Read", "read_file", "list_dir"]) expect(stepKind(name)).toBe("read");
    for (const name of ["grep", "Grep", "Glob"]) expect(stepKind(name)).toBe("search");
    expect(stepKind("TodoWrite")).toBe("plan");
    expect(stepKind("some_future_tool")).toBe("tool");
  });

  it("titles a step with the command or path and keeps its raw status", () => {
    const run = toStep(tool("a", "Bash", "running", { command: "pytest -q", description: "run tests" }));
    expect(run).toMatchObject({ kind: "command", status: "running", detail: "pytest -q" });
    const wrote = toStep(tool("b", "Write", "completed", { file_path: "/w/a.py", content: "x = 1\n" }, { output: "ok" }));
    expect(wrote).toMatchObject({ kind: "edit", status: "done", detail: "/w/a.py", output: "ok" });
    expect(toStep(tool("c", "shell", "completed", "ls -la")).detail).toBe("ls -la");
    expect(toStep(tool("d", "bash", "completed", {}, { error: true })).status).toBe("error");
    expect(toStep(tool("e", "bash", "error")).status).toBe("error");
    expect(toStep(tool("f", "mystery", "completed", {}, { title: "did a thing" })).detail).toBe("did a thing");
  });

  it("shows tool input as readable arguments, not escaped JSON, without repeating the title", () => {
    const shown = inputText({ file_path: "/w/a.py", content: 'print("hi")\nprint("there")\n', overwrite: true }, "/w/a.py");
    expect(shown).toBe('content:\nprint("hi")\nprint("there")\n\n\noverwrite: true');
    expect(shown).not.toContain("\\n");
    expect(inputText("ls -la")).toBe("ls -la");
    expect(inputText(undefined)).toBe("");
  });

  it("keeps prose as prose and groups consecutive tools and reasoning into work", () => {
    const m = message([
      { key: "r1", kind: "reasoning", revision: 1, content: "Plan the steps" },
      tool("1", "Read", "completed", { file_path: "a.py" }),
      tool("2", "Bash", "completed", { command: "ls" }),
      text("p1", "I looked around."),
      tool("3", "Write", "running", { file_path: "b.py" }),
      { key: "r2", kind: "reasoning", revision: 1, content: "   " },
    ]);
    const entries = entriesOf(m, true);
    expect(entries.map((e) => e.type)).toEqual(["work", "text", "work"]);
    const [first, , last] = entries;
    expect(first.type === "work" && first.steps.map((s) => s.kind)).toEqual(["thought", "read", "command"]);
    expect(first.type === "work" && first.running).toBe(false);
    expect(last.type === "work" && last.running).toBe(true);
    expect(first.type === "work" && countSteps(first.steps)).toEqual({ thought: 1, read: 1, command: 1 });
  });

  it("never leaves a spinner on a tool once the Turn is no longer live", () => {
    const m = message([tool("1", "Bash", "running", { command: "sleep 999" })]);
    const [entry] = entriesOf(m, false);
    expect(entry.type === "work" && entry.running).toBe(false);
    expect(entry.type === "work" && entry.steps[0].status).toBe("done");
    // While live, the newest group is where the agent is working even between tool calls.
    const [live] = entriesOf(message([tool("1", "Bash", "completed", { command: "ls" })]), true);
    expect(live.type === "work" && live.running).toBe(true);
  });

  it("falls back to authored content for Messages without parts", () => {
    const entries = entriesOf(message([], { content: [{ kind: "text", text: "hello" }] }), false);
    expect(entries).toEqual([{ type: "text", key: "m1:content", content: "hello", streaming: false }]);
  });

  it("folds consecutive finished reads, searches and edits but never a running or failed step", () => {
    const steps = [
      tool("1", "Read", "completed", { file_path: "src/a.py" }),
      tool("2", "Read", "completed", { file_path: "src/b.py" }),
      tool("3", "Read", "completed", { file_path: "src/a.py" }),
      tool("4", "Grep", "completed", { pattern: "def " }),
      tool("5", "Glob", "completed", { pattern: "**/*.py" }),
      tool("6", "Bash", "completed", { command: "ls" }),
      tool("7", "Bash", "completed", { command: "pytest" }),
      tool("8", "Edit", "completed", { file_path: "src/a.py" }),
      tool("9", "Edit", "error", { file_path: "src/b.py" }, { error: true }),
      tool("10", "Write", "completed", { file_path: "src/c.py" }),
      tool("11", "Write", "running", { file_path: "src/d.py" }),
      tool("12", "mcp__x__lookup", "completed", { query: "a" }),
      tool("13", "mcp__x__lookup", "completed", { query: "b" }),
      tool("14", "mcp__y__other", "completed", { query: "c" }),
    ].map(toStep);
    const rows = rowsOf(steps);
    expect(rows.map((r) => [r.kind, r.steps.length])).toEqual([
      ["read", 3],
      ["search", 2],
      ["command", 1], // commands are the work itself: each keeps its own line
      ["command", 1],
      ["edit", 1],
      ["edit", 1], // the failed edit stays visible on its own
      ["edit", 1],
      ["edit", 1], // and so does the one still running
      ["tool", 2], // repeats of the same tool fold; a different tool does not join
      ["tool", 1],
    ]);
    // Folding regroups, it never drops or reorders a step.
    expect(rows.flatMap((r) => r.steps.map((s) => s.key))).toEqual(steps.map((s) => s.key));
    expect(namesOf(rows[0])).toEqual(["a.py", "b.py"]);
    // Row keys depend only on the first step, so a growing fold keeps its open/closed state.
    expect(rowsOf(steps.slice(0, 2))[0].key).toBe(rows[0].key);
  });

  it("marks text and reasoning as streaming only while deltas keep arriving here", () => {
    const now = 1_000_000;
    const fresh: MessagePart = { key: "p1", kind: "text", revision: 4, content: "Hel", seen_at: now - 300 };
    expect(isStreaming(fresh, now)).toBe(true);
    expect(isStreaming({ ...fresh, seen_at: now - 5000 }, now)).toBe(false); // the stream went quiet
    expect(isStreaming({ ...fresh, seen_at: undefined }, now)).toBe(false); // loaded from a snapshot
    expect(isStreaming({ ...fresh, sealed: true }, now)).toBe(false);
    const thought: MessagePart = { key: "r1", kind: "reasoning", revision: 2, content: "Hmm", seen_at: now - 100 };
    const [work] = entriesOf(message([thought]), true, now);
    expect(work.type === "work" && work.steps[0].streaming).toBe(true);
    expect(work.type === "work" && work.running).toBe(true);
    // Only the newest part can be growing; an older one is done even if it was just seen.
    const [older, newest] = entriesOf(message([{ ...fresh }, { ...fresh, key: "p2" }]), true, now);
    expect([older.type === "text" && older.streaming, newest.type === "text" && newest.streaming]).toEqual([false, true]);
    // Nothing streams once the Turn is over.
    const [done] = entriesOf(message([fresh]), false, now);
    expect(done.type === "text" && done.streaming).toBe(false);
  });

  it("reports the live activity from the newest part and never guesses", () => {
    const now = 1_000_000;
    expect(activityOf([], now)).toEqual({ type: "waiting" });
    expect(activityOf([message([tool("1", "Bash", "running", { command: "pytest -q" })])], now)).toEqual({
      type: "step",
      kind: "command",
      detail: "pytest -q",
    });
    expect(activityOf([message([tool("1", "Bash", "completed", { command: "ls" })])], now)).toEqual({ type: "waiting" });
    expect(activityOf([message([{ key: "p", kind: "text", revision: 2, content: "Hi", seen_at: now - 10 }])], now)).toEqual({ type: "writing" });
    expect(activityOf([message([{ key: "r", kind: "reasoning", revision: 2, content: "Hm", seen_at: now - 10 }])], now)).toEqual({ type: "thinking" });
    // A whole block from a CLI that does not stream is not "being written".
    expect(activityOf([message([text("p", "Complete block")])], now)).toEqual({ type: "waiting" });
  });

  it("measures a group from recorded part times only", () => {
    const at = (key: string, created_at: string | null, updated_at: string | null) =>
      toStep({ ...tool(key, "Bash", "completed", { command: "x" }), created_at, updated_at });
    expect(spanOf([at("1", "2026-10-10T00:00:05Z", "2026-10-10T00:00:07Z"), at("2", "2026-10-10T00:00:01Z", "2026-10-10T00:00:30Z")])).toEqual({
      start: "2026-10-10T00:00:01Z",
      end: "2026-10-10T00:00:30Z",
    });
    expect(spanOf([at("1", null, null)])).toEqual({ start: null, end: null });
  });

  it("groups Messages under their Turn and keeps Turns that have no Message yet", () => {
    const user = message([], { id: "u1", ordinal: 1, role: "user", content: [{ kind: "text", text: "do it" }] });
    const reply = message([text("p", "done")], { id: "a1", ordinal: 2 });
    const note = message([], { id: "n1", ordinal: 3, role: "user", turn_id: null, routing: "note" });
    const blocks = blocksOf([user, reply, note], [turn(), turn({ id: "t2", ordinal: 2, state: "queued" })]);
    expect(blocks.map((b) => [b.turn?.id ?? null, b.messages.map((m) => m.id)])).toEqual([
      ["t1", ["u1", "a1"]],
      [null, ["n1"]],
      ["t2", []],
    ]);
  });

  it("formats only real measurements", () => {
    expect(secondsBetween("2026-01-01T00:00:00Z", "2026-01-01T00:01:12Z")).toBe(72);
    expect(secondsBetween(null, "2026-01-01T00:01:12Z")).toBeNull();
    expect(secondsBetween("2026-01-01T00:01:12Z", "2026-01-01T00:00:00Z")).toBeNull();
    expect([formatDuration(9), formatDuration(72), formatDuration(3725)]).toEqual(["9s", "1m 12s", "1h 02m"]);
    expect([formatTokens(761), formatTokens(20604), formatTokens(153000)]).toEqual(["761", "20.6k", "153k"]);
  });

  it("bounds long output but reports exactly how much is hidden", () => {
    const long = Array.from({ length: 40 }, (_, i) => `line ${i + 1}`).join("\n");
    const { preview, hidden } = previewOf(long);
    expect(preview.split("\n")).toHaveLength(14);
    expect(hidden).toBe(26);
    expect(previewOf("a\nb\n")).toEqual({ preview: "a\nb", hidden: 0 });
  });
});

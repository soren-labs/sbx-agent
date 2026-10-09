import type { Message, MessagePart, Turn } from "../../api/types";

/**
 * Presentation model of the Session worklog. Pure functions over the server's
 * Messages, parts and Turns: nothing here invents state, durations or outcomes.
 */

export type StepKind = "command" | "edit" | "read" | "search" | "web" | "plan" | "agent" | "thought" | "tool";
export type StepStatus = "running" | "done" | "error";

export interface Step {
  key: string;
  kind: StepKind;
  status: StepStatus;
  /** Tool name exactly as the CLI reported it. */
  name: string;
  /** Command line, file path or query when the CLI reported one. */
  detail: string;
  input: string;
  output: string;
}

export type Entry =
  | { type: "text"; key: string; content: string }
  | { type: "work"; key: string; steps: Step[]; running: boolean };

// Tool names differ per official CLI; these cover OpenCode, Codex, Claude Code,
// Grok Build and Command Code. Anything else stays a generic "tool" step.
const KINDS: [StepKind, RegExp][] = [
  ["command", /^(bash|shell|shell_command|run_terminal_command|exec|command_execution|terminal)$/i],
  ["edit", /^(write|write_file|edit|edit_file|multiedit|search_replace|apply_patch|patch|file_change|create_file|str_replace|notebookedit)$/i],
  ["read", /^(read|read_file|list_dir|ls|list|view|cat)$/i],
  ["search", /^(grep|glob|search|find|codesearch|search_tool|rg)$/i],
  ["web", /^(webfetch|web_fetch|websearch|web_search|fetch)$/i],
  ["plan", /^(todowrite|todo_write|todoread|update_plan|plan|enter_plan_mode|exit_plan_mode)$/i],
  ["agent", /^(task|agent|spawn_subagent|use_tool|workflow)$/i],
];

export function stepKind(name: string): StepKind {
  return KINDS.find(([, re]) => re.test(name))?.[0] ?? "tool";
}

function text(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

const DETAIL_KEYS = ["command", "cmd", "file_path", "filePath", "path", "pattern", "query", "url", "description", "prompt"];

function detailOf(data: Record<string, unknown>): string {
  const input = data.input;
  if (input && typeof input === "object" && !Array.isArray(input)) {
    for (const key of DETAIL_KEYS) {
      const value = (input as Record<string, unknown>)[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
  }
  if (typeof input === "string" && input.trim()) return input.trim();
  return typeof data.title === "string" ? data.title.trim() : "";
}

/**
 * Tool input as people read it: one labelled block per argument with real line breaks
 * (file contents, patches), not an escaped JSON string. The argument already shown as
 * the step title is not repeated.
 */
export function inputText(input: unknown, shown = ""): string {
  if (!input || typeof input !== "object" || Array.isArray(input)) return text(input);
  const blocks: string[] = [];
  for (const [key, value] of Object.entries(input as Record<string, unknown>)) {
    if (value === undefined || value === null || value === "") continue;
    if (typeof value === "string" && value.trim() === shown) continue;
    const body = typeof value === "string" ? value : text(value);
    blocks.push(body.includes("\n") ? `${key}:\n${body}` : `${key}: ${body}`);
  }
  return blocks.join("\n\n");
}

export function toStep(part: MessagePart): Step {
  if (part.kind === "reasoning") {
    return { key: part.key, kind: "thought", status: "done", name: "reasoning", detail: "", input: "", output: part.content };
  }
  const data = (part.data ?? {}) as Record<string, unknown>;
  const name = String(data.name ?? "tool");
  const raw = String(data.status ?? "");
  const status: StepStatus =
    data.error === true || raw === "error" || raw === "failed" ? "error" : raw === "completed" ? "done" : "running";
  const detail = detailOf(data);
  return { key: part.key, kind: stepKind(name), status, name, detail, input: inputText(data.input, detail), output: text(data.output) };
}

/**
 * Parts in server order: prose stays prose, consecutive tool/reasoning parts become
 * one work group. A group is running only while the Turn itself is live — a tool the
 * CLI never closed must not spin forever after the Turn ended.
 */
export function entriesOf(message: Message, live: boolean): Entry[] {
  const entries: Entry[] = [];
  let group: Extract<Entry, { type: "work" }> | null = null;
  for (const part of message.parts) {
    if (part.kind === "tool" || part.kind === "reasoning") {
      if (part.kind === "reasoning" && !part.content.trim()) continue;
      if (!group) {
        group = { type: "work", key: `work:${part.key}`, steps: [], running: false };
        entries.push(group);
      }
      const step = toStep(part);
      group.steps.push(live || step.status !== "running" ? step : { ...step, status: "done" });
    } else if (part.content.trim()) {
      group = null;
      entries.push({ type: "text", key: part.key, content: part.content });
    }
  }
  if (!message.parts.length) {
    const authored = message.content.map((c) => c.text ?? "").join("\n\n");
    if (authored.trim()) entries.push({ type: "text", key: `${message.id}:content`, content: authored });
  }
  for (const entry of entries) {
    if (entry.type === "work") entry.running = live && entry.steps.some((s) => s.status === "running");
  }
  // While the Turn is live the newest group is where the agent is working.
  const last = entries[entries.length - 1];
  if (live && last?.type === "work") last.running = true;
  return entries;
}

export type Counts = Partial<Record<StepKind, number>>;

export function countSteps(steps: Step[]): Counts {
  const counts: Counts = {};
  for (const step of steps) counts[step.kind] = (counts[step.kind] ?? 0) + 1;
  return counts;
}

export const LIVE_TURN_STATES = ["queued", "preparing", "running"];
export const isLiveTurn = (turn: Turn | undefined) => !!turn && LIVE_TURN_STATES.includes(turn.state);

export interface TurnBlock {
  turn: Turn | null;
  messages: Message[];
}

/** Messages grouped under the Turn they belong to, in conversation order. */
export function blocksOf(messages: Message[], turns: Turn[]): TurnBlock[] {
  const byId = new Map(turns.map((t) => [t.id, t]));
  const blocks: TurnBlock[] = [];
  const seen = new Set<string>();
  for (const message of messages) {
    const turn = message.turn_id ? (byId.get(message.turn_id) ?? null) : null;
    const prev = blocks[blocks.length - 1];
    if (turn && prev?.turn?.id === turn.id) {
      prev.messages.push(message);
    } else {
      blocks.push({ turn, messages: [message] });
      if (turn) seen.add(turn.id);
    }
  }
  // A Turn the server knows about before any of its Messages arrived.
  for (const turn of turns) if (!seen.has(turn.id)) blocks.push({ turn, messages: [] });
  return blocks;
}

/** Whole seconds between two server timestamps; null when either is missing. */
export function secondsBetween(start?: string | null, end?: string | null): number | null {
  if (!start || !end) return null;
  const ms = new Date(end).getTime() - new Date(start).getTime();
  return Number.isFinite(ms) && ms >= 0 ? Math.round(ms / 1000) : null;
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${String(seconds % 60).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

export function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  return n < 100_000 ? `${(n / 1000).toFixed(1)}k` : `${Math.round(n / 1000)}k`;
}

/** Long outputs are shown bounded first; the rest is one click away, never dropped. */
export const OUTPUT_PREVIEW_LINES = 14;

export function previewOf(output: string): { preview: string; hidden: number } {
  const lines = output.replace(/\n+$/, "").split("\n");
  if (lines.length <= OUTPUT_PREVIEW_LINES + 2) return { preview: lines.join("\n"), hidden: 0 };
  return { preview: lines.slice(0, OUTPUT_PREVIEW_LINES).join("\n"), hidden: lines.length - OUTPUT_PREVIEW_LINES };
}

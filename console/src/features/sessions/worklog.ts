import type { Message, MessagePart, Turn } from "../../api/types";

/**
 * Presentation model of the Session worklog. Pure functions over the server's
 * Messages, parts and Turns: nothing here invents state, durations or outcomes.
 */

export type StepKind = "command" | "edit" | "read" | "search" | "web" | "plan" | "agent" | "thought" | "note" | "tool";
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
  /** Server timestamps of the part; absent when the server never recorded them. */
  startedAt?: string | null;
  endedAt?: string | null;
  /** Reasoning the provider is still streaming. */
  streaming?: boolean;
}

export type Entry =
  | { type: "text"; key: string; content: string; streaming: boolean }
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
  const times = { startedAt: part.created_at, endedAt: part.updated_at };
  if (part.kind === "reasoning") {
    return { key: part.key, kind: "thought", status: "done", name: "reasoning", detail: "", input: "", output: part.content, ...times };
  }
  const data = (part.data ?? {}) as Record<string, unknown>;
  const name = String(data.name ?? "tool");
  const raw = String(data.status ?? "");
  const status: StepStatus =
    data.error === true || raw === "error" || raw === "failed" ? "error" : raw === "completed" ? "done" : "running";
  const detail = detailOf(data);
  return { key: part.key, kind: stepKind(name), status, name, detail, input: inputText(data.input, detail), output: text(data.output), ...times };
}

/**
 * Parts in server order: prose stays prose, consecutive tool/reasoning parts become
 * one work group. A group is running only while the Turn itself is live — a tool the
 * CLI never closed must not spin forever after the Turn ended.
 */
/**
 * A part counts as streaming only while this browser keeps receiving changes to it.
 * CLIs that report whole blocks never look like they are typing, and a reload does
 * not pretend an old part is growing.
 */
export const STREAM_FRESH_MS = 2500;

export function isStreaming(part: MessagePart | undefined, now: number): boolean {
  return !!part && !part.sealed && part.kind !== "tool" && part.seen_at !== undefined && now - part.seen_at < STREAM_FRESH_MS;
}

export function entriesOf(message: Message, live: boolean, now = Date.now()): Entry[] {
  const entries: Entry[] = [];
  let group: Extract<Entry, { type: "work" }> | null = null;
  const shown = message.parts.filter((part) => part.kind === "tool" || part.content.trim());
  // Only the newest part can still be growing: the provider generates in order.
  const last = live ? shown[shown.length - 1] : undefined;
  const growing = isStreaming(last, now) ? last : undefined;
  // Text the agent says between two tool calls is narration of the work, not an answer:
  // it stays in the work group, in order and in full. The opening line (before any
  // tool) and the closing text (after the last one) remain prose.
  const firstTool = shown.findIndex((part) => part.kind === "tool");
  const lastTool = shown.reduce((at, part, index) => (part.kind === "tool" ? index : at), -1);
  shown.forEach((part, index) => {
    const narration = part.kind === "text" && index > firstTool && index < lastTool && firstTool >= 0;
    if (part.kind === "tool" || part.kind === "reasoning" || narration) {
      if (!group) {
        group = { type: "work", key: `work:${part.key}`, steps: [], running: false };
        entries.push(group);
      }
      if (narration) {
        const times = { startedAt: part.created_at, endedAt: part.updated_at };
        group.steps.push({ key: part.key, kind: "note", status: "done", name: "text", detail: "", input: "", output: part.content, ...times });
        return;
      }
      const step = toStep(part);
      if (part.kind === "reasoning") step.streaming = part === growing;
      group.steps.push(live || step.status !== "running" ? step : { ...step, status: "done" });
    } else {
      group = null;
      entries.push({ type: "text", key: part.key, content: part.content, streaming: part === growing });
    }
  });
  if (!message.parts.length) {
    const authored = message.content.map((c) => c.text ?? "").join("\n\n");
    if (authored.trim()) entries.push({ type: "text", key: `${message.id}:content`, content: authored, streaming: false });
  }
  for (const entry of entries) {
    if (entry.type === "work") entry.running = live && entry.steps.some((s) => s.status === "running" || s.streaming);
  }
  // While the Turn is live the newest group is where the agent is working.
  const tail = entries[entries.length - 1];
  if (live && tail?.type === "work") tail.running = true;
  return entries;
}

/**
 * One line of the worklog. Consecutive finished steps of the same quiet kind (reads,
 * searches, edits, …) fold into a single row; a running or failed step always keeps
 * its own row so it cannot hide inside a fold. Folding only regroups: every step is
 * still there, in order, one click away.
 */
export interface Row {
  key: string;
  kind: StepKind;
  steps: Step[];
}

const FOLDABLE: StepKind[] = ["read", "search", "edit", "web", "plan", "tool"];

function folds(prev: Step, step: Step): boolean {
  if (prev.kind !== step.kind || !FOLDABLE.includes(step.kind)) return false;
  if (prev.status !== "done" || step.status !== "done") return false;
  // Unclassified tools fold only with repeats of the same tool.
  return step.kind !== "tool" || prev.name === step.name;
}

export function rowsOf(steps: Step[]): Row[] {
  const rows: Row[] = [];
  for (const step of steps) {
    const row = rows[rows.length - 1];
    if (row && folds(row.steps[row.steps.length - 1], step)) row.steps.push(step);
    else rows.push({ key: `row:${step.key}`, kind: step.kind, steps: [step] });
  }
  return rows;
}

/** First line of Markdown as plain words, for a one-line preview; the full text stays in the step. */
export function plainLine(markdown: string): string {
  const line = markdown.trim().split("\n")[0];
  return line
    .replace(/^#{1,6}\s+|^[-*+]\s+|^>\s+/, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/(\*\*|__|`|~~)/g, "")
    .replace(/(^|\s)[*_](\S[^*_]*\S|\S)[*_](?=\s|[.,;:!?)]|$)/g, "$1$2");
}

/** Last path segment, for compact lists of files; the full path stays in the step. */
export function shortName(detail: string): string {
  const clean = detail.replace(/[\\/]+$/, "");
  return clean.slice(Math.max(clean.lastIndexOf("/"), clean.lastIndexOf("\\")) + 1) || detail;
}

/** Distinct short names of a folded row, in order. */
export function namesOf(row: Row): string[] {
  return [...new Set(row.steps.map((step) => shortName(step.detail || step.name)))];
}

/** First recorded start and last recorded end across steps; null when unrecorded. */
export function spanOf(steps: Step[]): { start: string | null; end: string | null } {
  let start: string | null = null;
  let end: string | null = null;
  for (const step of steps) {
    if (step.startedAt && (!start || step.startedAt < start)) start = step.startedAt;
    const last = step.endedAt ?? step.startedAt;
    if (last && (!end || last > end)) end = last;
  }
  return { start, end };
}

/** Rows shown while a group is live: the newest ones, so the current step stays in view. */
export const LIVE_TAIL_ROWS = 8;

/** What the agent is doing right now, from the newest part of a live Turn. */
export type Activity =
  | { type: "step"; kind: StepKind; detail: string }
  | { type: "thinking" }
  | { type: "writing" }
  | { type: "waiting" };

export function activityOf(messages: Message[], now = Date.now()): Activity {
  const parts = messages.filter((m) => m.role === "assistant").flatMap((m) => m.parts);
  const last = parts[parts.length - 1];
  if (!last) return { type: "waiting" };
  if (last.kind === "tool") {
    const step = toStep(last);
    return step.status === "running" ? { type: "step", kind: step.kind, detail: step.detail || step.name } : { type: "waiting" };
  }
  if (!isStreaming(last, now)) return { type: "waiting" };
  return last.kind === "reasoning" ? { type: "thinking" } : { type: "writing" };
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

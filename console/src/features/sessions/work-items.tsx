import { useEffect, useState, type MouseEvent, type ReactNode } from "react";
import {
  Bot,
  Brain,
  ChevronRight,
  FilePen,
  FileText,
  Globe,
  ListChecks,
  Search,
  StickyNote,
  Terminal,
  Wrench,
  X,
} from "lucide-react";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { Spinner } from "../../ui/Spinner";
import { Markdown } from "../../ui/Markdown";
import {
  LIVE_TAIL_ROWS,
  countSteps,
  entriesOf,
  formatDuration,
  namesOf,
  plainLine,
  previewOf,
  rowsOf,
  secondsBetween,
  spanOf,
  toolsOf,
  type Entry,
  type Row,
  type Step,
  type StepKind,
} from "./worklog";

export { entriesOf };

type T = ReturnType<typeof useI18n>["t"];

export const STEP_ICON: Record<StepKind, ReactNode> = {
  command: <Terminal size={13} />,
  edit: <FilePen size={13} />,
  read: <FileText size={13} />,
  search: <Search size={13} />,
  web: <Globe size={13} />,
  plan: <ListChecks size={13} />,
  agent: <Bot size={13} />,
  thought: <Brain size={13} />,
  note: <StickyNote size={13} />,
  tool: <Wrench size={13} />,
};

export function clock(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Seconds since `start`, ticking while `running`. */
export function useElapsed(start: string | null | undefined, running: boolean): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running]);
  if (!start) return null;
  const ms = now - new Date(start).getTime();
  return Number.isFinite(ms) ? Math.max(0, Math.round(ms / 1000)) : null;
}

/** How long a live work group waits for the next tool before it reads as finished. */
const SETTLE_MS = 1500;

/** `on`, held for `ms` after it turns off, so a state that flips back at once never flickers. */
function useLinger(on: boolean, ms: number): boolean {
  const [held, setHeld] = useState(on);
  useEffect(() => {
    if (on || !ms) return setHeld(on);
    const id = setTimeout(() => setHeld(false), ms);
    return () => clearTimeout(id);
  }, [on, ms]);
  return on || (held && ms > 0);
}

/** Bounded output: the first lines are visible, the rest is one click away. */
function Output({ label, value, tone }: { label: string; value: string; tone?: "error" }) {
  const { t } = useI18n();
  const [all, setAll] = useState(false);
  const { preview, hidden } = previewOf(value);
  return (
    <div className={`step-io${tone === "error" ? " step-io--error" : ""}`}>
      <div className="step-io-label">{label}</div>
      <pre tabIndex={0}>{all ? value : preview}</pre>
      {hidden > 0 ? (
        <button type="button" className="step-more" aria-expanded={all} onClick={() => setAll(!all)}>
          {all ? t("work.show_less") : t("work.show_more", { n: hidden })}
        </button>
      ) : null}
    </div>
  );
}

function StepDetail({ step }: { step: Step }) {
  const { t } = useI18n();
  // The command/path is already the row title; show raw input only when it adds something.
  const input = step.input && step.input.trim() !== step.detail ? step.input : "";
  return (
    <div className="step-body">
      {/* The exact command or path, never the shortened row title. */}
      {step.detail ? <Output label={step.name} value={step.detail} /> : null}
      {input ? <Output label={t("work.input")} value={input} /> : null}
      {step.output ? (
        <Output label={t("work.output")} value={step.output} tone={step.status === "error" ? "error" : undefined} />
      ) : step.status !== "running" ? (
        <p className="step-empty">{t("work.no_output")}</p>
      ) : null}
    </div>
  );
}

function StepState({ step }: { step: Step }) {
  const { t } = useI18n();
  if (step.status === "running")
    return (
      <span className="step-state step-state--live" role="img" aria-label={t("work.step.running")}>
        <Spinner small />
      </span>
    );
  if (step.status === "error")
    return (
      <span className="step-state step-state--error">
        <X size={12} />
        {t("work.step.error")}
      </span>
    );
  return null;
}

function stepDuration(step: Step): string | null {
  const took = secondsBetween(step.startedAt, step.endedAt);
  return took !== null && took > 0 ? formatDuration(took) : null;
}

/** One tool call: verb + the real command or path; the full input/output is a click away. */
function StepRow({ step, nested }: { step: Step; nested?: boolean }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const title = step.detail || step.name;
  const dur = stepDuration(step);
  return (
    <li
      className={`step step-${step.status} ${nested ? "is-nested" : ""}`}
      data-kind={step.kind}
      data-row={nested ? undefined : step.key}
      data-testid="work-step"
    >
      <button type="button" className="step-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="step-icon" aria-hidden="true">
          {STEP_ICON[step.kind]}
        </span>
        <span className="step-kind">{t(`work.kind.${step.kind}` as I18nKey)}</span>
        <span className="step-title" title={title}>
          {title}
        </span>
        <StepState step={step} />
        {dur ? <span className="step-dur faint">{dur}</span> : null}
        <ChevronRight size={12} className={`step-chevron ${open ? "open" : ""}`} />
      </button>
      {open ? <StepDetail step={step} /> : null}
    </li>
  );
}

/** Provider-visible reasoning: open while it streams, folded once the model moves on. */
function ThoughtRow({ step }: { step: Step }) {
  const { t } = useI18n();
  const [choice, setChoice] = useState<boolean | null>(null);
  const open = choice ?? !!step.streaming;
  const took = secondsBetween(step.startedAt, step.endedAt);
  const label = step.streaming
    ? t("work.thinking")
    : took
      ? t("work.thought_for", { time: formatDuration(took) })
      : t("work.kind.thought");
  return (
    <li className={`step step-thought-row ${step.streaming ? "is-streaming" : ""}`} data-kind="thought" data-testid="work-step">
      <button type="button" className="step-head" aria-expanded={open} onClick={() => setChoice(!open)}>
        <span className="step-icon" aria-hidden="true">
          {STEP_ICON.thought}
        </span>
        <span className="step-kind">{label}</span>
        {open ? <span className="step-title" /> : <span className="step-title step-title-quiet">{step.output.trim().split("\n")[0]}</span>}
        <ChevronRight size={12} className={`step-chevron ${open ? "open" : ""}`} />
      </button>
      {/* Reasoning is plain text from the model: not Markdown (it mangles __names__). */}
      {open ? <div className="step-body step-thought">{step.output.trim()}</div> : null}
    </li>
  );
}

/** What the agent said between two tool calls: its own words, quiet, complete on click. */
function NoteRow({ step }: { step: Step }) {
  const [open, setOpen] = useState(false);
  const text = step.output.trim();
  return (
    <li className="step step-note" data-kind="note" data-testid="work-note">
      <button type="button" className="step-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="step-icon" aria-hidden="true">
          {STEP_ICON.note}
        </span>
        <span className="step-title step-title-quiet">{plainLine(text)}</span>
        <ChevronRight size={12} className={`step-chevron ${open ? "open" : ""}`} />
      </button>
      {open ? (
        <div className="step-body">
          <Markdown text={text} />
        </div>
      ) : null}
    </li>
  );
}

/** Several finished steps of one kind on a single line; expanding lists every one. */
function FoldRow({ row }: { row: Row }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const names = namesOf(row);
  const count = toolsOf(row).length;
  const label =
    row.kind === "tool"
      ? t("work.fold.tool", { n: count, name: row.steps[0].name })
      : t(`work.fold.${row.kind}` as I18nKey, { n: row.kind === "edit" || row.kind === "read" ? names.length : count });
  return (
    <li className="step step-fold" data-kind={row.kind} data-testid="work-fold">
      <button type="button" className="step-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="step-icon" aria-hidden="true">
          {STEP_ICON[row.kind]}
        </span>
        <span className="step-kind">{label}</span>
        <span className="step-title step-title-quiet" title={names.join(", ")}>
          {row.kind === "tool" ? "" : names.join(", ")}
        </span>
        <ChevronRight size={12} className={`step-chevron ${open ? "open" : ""}`} />
      </button>
      {open ? (
        <ol className="work-steps is-nested">
          {row.steps.map((step) =>
            step.kind === "thought" ? (
              <ThoughtRow key={step.key} step={step} />
            ) : step.kind === "note" ? (
              <NoteRow key={step.key} step={step} />
            ) : (
              <StepRow key={step.key} step={step} nested />
            ),
          )}
        </ol>
      ) : null}
    </li>
  );
}

function RowItem({ row }: { row: Row }) {
  if (row.steps.length > 1) return <FoldRow row={row} />;
  if (row.kind === "note") return <NoteRow step={row.steps[0]} />;
  return row.kind === "thought" ? <ThoughtRow step={row.steps[0]} /> : <StepRow step={row.steps[0]} />;
}

function plural(t: T, kind: StepKind, n: number): string {
  return t(`work.count.${kind}${n === 1 ? ".one" : ""}` as I18nKey, { n });
}

function summarize(t: T, steps: Step[]): string {
  const counts = countSteps(steps);
  const order: StepKind[] = ["command", "edit", "read", "search", "web", "plan", "agent", "tool"];
  return order
    .filter((kind) => counts[kind])
    .map((kind) => plural(t, kind, counts[kind]!))
    .join(" · ");
}

export function WorkGroup({ entry }: { entry: Extract<Entry, { type: "work" }> }) {
  const { t } = useI18n();
  // Open while the agent is working here; the user's own toggle wins afterwards.
  const [choice, setChoice] = useState<boolean | null>(null);
  // Row the reader pinned as the first visible one ("" = from the start): once they
  // touch the list, rows stop sliding out of view under them.
  const [pinned, setPinned] = useState<string | null>(null);
  // Single rows the reader clicked keep their own row, so a later neighbour of the
  // same kind does not fold them away together with what they opened.
  const [apart, setApart] = useState<ReadonlySet<string>>(() => new Set());
  // A line of narration after a tool is not the end of the work: while the Turn is
  // live the group stays as it is for a moment instead of closing and reopening.
  const running = useLinger(entry.running, entry.live ? SETTLE_MS : 0);
  const touch = (event: MouseEvent<HTMLOListElement>) => {
    // Reading inside the group is a choice to keep it open.
    if (choice === null) setChoice(true);
    if (pinned === null) setPinned(rows[hidden]?.key ?? "");
    const key = (event.target as HTMLElement).closest<HTMLElement>("[data-row]")?.dataset.row;
    if (key && !apart.has(key)) setApart(new Set(apart).add(key));
  };
  const failed = entry.steps.filter((s) => s.status === "error").length;
  const open = choice ?? running;
  const span = spanOf(entry.steps);
  const elapsed = useElapsed(span.start, running);
  const took = secondsBetween(span.start, span.end);
  // Reasoning with no tool around it is context, not work: a single quiet line.
  const solo = entry.steps.length === 1 && entry.steps[0].kind === "thought";
  if (solo) {
    return (
      <ol className="work-steps is-solo" data-testid="work-group">
        <ThoughtRow step={entry.steps[0]} />
      </ol>
    );
  }
  const rows = rowsOf(entry.steps, apart);
  const hidden = !running
    ? 0
    : pinned === null
      ? Math.max(0, rows.length - LIVE_TAIL_ROWS)
      : // A pinned row may since have folded into its neighbour: find it by its step.
        Math.max(0, rows.findIndex((row) => row.steps.some((step) => `row:${step.key}` === pinned)));
  const head = running
    ? elapsed !== null
      ? t("work.working_for", { time: formatDuration(elapsed) })
      : t("work.working")
    : took
      ? t("work.worked_for", { time: formatDuration(took) })
      : t("work.worked");
  return (
    <section className={`work ${running ? "is-running" : ""} ${open ? "is-open" : ""}`} data-testid="work-group">
      <button type="button" className="work-head" aria-expanded={open} onClick={() => setChoice(!open)}>
        {running ? <span className="work-dot" aria-hidden="true" /> : <ChevronRight size={13} className={`step-chevron ${open ? "open" : ""}`} />}
        <strong>{head}</strong>
        <span className="work-summary">{summarize(t, entry.steps)}</span>
        {failed ? <span className="work-failed">{t("work.failed_steps", { n: failed })}</span> : null}
      </button>
      {open ? (
        <ol className="work-steps" onClickCapture={touch}>
          {hidden ? (
            <li className="step">
              <button type="button" className="step-more" onClick={() => setPinned("")}>
                {t(hidden === 1 ? "work.earlier.one" : "work.earlier", { n: hidden })}
              </button>
            </li>
          ) : null}
          {rows.slice(hidden).map((row) => (
            <RowItem key={row.key} row={row} />
          ))}
        </ol>
      ) : null}
    </section>
  );
}
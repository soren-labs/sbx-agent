import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type FormEvent } from "react";
import { Plus, SquareTerminal, X } from "lucide-react";
import type { TerminalInfo } from "../../../api/types";
import { isApiError } from "../../../api/errors";
import { useI18n } from "../../../i18n";
import { useApi, useQueryClient } from "../../../state/context";
import { useQuery } from "../../../state/query";
import { Button } from "../../../ui/Button";
import { ErrorNotice } from "../../../ui/ErrorNotice";
import { Field, Input } from "../../../ui/Field";
import { Pill } from "../../../ui/Pill";
import { useAction } from "../../../ui/useAction";
import { TerminalScreen, type Style } from "../terminal-screen";

interface View {
  screen: TerminalScreen;
  offset: number;
  closed: boolean;
}

interface SessionTerminals {
  selected: string | null;
  views: Map<string, View>;
}

/**
 * What this page has already rendered per Session, so switching workspace panels (or
 * Sessions) re-attaches to the same terminal without re-reading its output. The runtime
 * owns the processes and their output buffers; after a reload the views are rebuilt from
 * `GET /terminals` and the runtime's buffer.
 */
const store = new Map<string, SessionTerminals>();
const MAX_SESSIONS = 20;

export function terminalsOf(sessionId: string): SessionTerminals {
  let entry = store.get(sessionId);
  if (!entry) {
    entry = { selected: null, views: new Map() };
    store.set(sessionId, entry);
    while (store.size > MAX_SESSIONS) store.delete(store.keys().next().value as string);
  }
  return entry;
}

/** Test hook: forget every rendered terminal. */
export function resetTerminalViews(): void {
  store.clear();
}

function view(entry: SessionTerminals, id: string): View {
  let v = entry.views.get(id);
  if (!v) {
    v = { screen: new TerminalScreen(), offset: 0, closed: false };
    entry.views.set(id, v);
  }
  return v;
}

function cssOf(style: Style): { className: string; style?: CSSProperties } {
  const cls: string[] = [];
  const css: CSSProperties = {};
  let fg = style.fg;
  let bg = style.bg;
  if (style.inverse) [fg, bg] = [bg ?? "default-bg", fg ?? "default-fg"];
  if (fg?.startsWith("#")) css.color = fg;
  else if (fg) cls.push(`term-fg-${fg}`);
  if (bg?.startsWith("#")) css.backgroundColor = bg;
  else if (bg) cls.push(`term-bg-${bg}`);
  if (style.bold) cls.push("term-bold");
  if (style.dim) cls.push("term-dim");
  if (style.italic) cls.push("term-italic");
  if (style.underline) cls.push("term-underline");
  return { className: cls.join(" "), style: Object.keys(css).length ? css : undefined };
}

function ScreenView({ screen, version, label }: { screen: TerminalScreen; version: number; label: string }) {
  const ref = useRef<HTMLPreElement>(null);
  const pinned = useRef(true);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [version]);
  const lines = screen.render();
  return (
    <pre
      ref={ref}
      className="terminal"
      role="log"
      aria-label={label}
      tabIndex={0}
      data-testid="terminal-screen"
      onScroll={(e) => {
        const el = e.currentTarget;
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
      }}
    >
      {lines.map((runs, i) => (
        <span key={screen.dropped + i} className="terminal__line">
          {runs.map((r, j) => {
            const { className, style } = cssOf(r.style);
            return className || style ? (
              <span key={j} className={className || undefined} style={style}>
                {r.text}
              </span>
            ) : (
              r.text
            );
          })}
          {"\n"}
        </span>
      ))}
    </pre>
  );
}

/** Lease-local PTYs: listed by the runtime, polled output, no restored-process claims. */
export function TerminalTab({
  sessionId,
  sessionClosed = false,
  pollMs = 1000,
}: {
  sessionId: string;
  sessionClosed?: boolean;
  pollMs?: number;
}) {
  const { t } = useI18n();
  const api = useApi();
  const entry = terminalsOf(sessionId);
  const [selected, setSelectedState] = useState<string | null>(entry.selected);
  const [version, setVersion] = useState(0);
  const [pollError, setPollError] = useState<unknown>(null);
  const [line, setLine] = useState("");
  /** The last command sent; if its delivery is unconfirmed it is resent only with the same key. */
  const [lastSent, setLastSent] = useState("");
  const qc = useQueryClient();
  // One cache entry per Session that always holds the latest server list: a remount must
  // never prune a running terminal on an older answer.
  const list = useQuery(sessionClosed ? null : ["terminals", sessionId], () => api.terminals.list(sessionId));
  const refreshList = useCallback(() => qc.invalidate(["terminals", sessionId]), [qc, sessionId]);
  const refetchList = list.refetch;
  useEffect(() => {
    if (!sessionClosed) refetchList();
  }, [sessionClosed, refetchList]);

  const select = useCallback(
    (id: string | null) => {
      terminalsOf(sessionId).selected = id;
      setSelectedState(id);
    },
    [sessionId],
  );

  // The runtime is the authority on which terminals exist: re-attach to a running one
  // instead of starting another, and forget views of terminals that are gone.
  const unlisted = isApiError(list.error) && list.error.code === "unsupported_capability";
  const listed: TerminalInfo[] | null = list.data?.items ?? null;
  useEffect(() => {
    if (!listed) return;
    for (const id of [...entry.views.keys()]) if (!listed.some((x) => x.terminal_id === id)) entry.views.delete(id);
    if (!selected || !listed.some((x) => x.terminal_id === selected)) {
      const next = listed.find((x) => !x.closed) ?? listed[listed.length - 1] ?? null;
      select(next?.terminal_id ?? null);
    }
  }, [listed, selected, entry, select]);

  const start = useAction(async (key) => {
    const r = await api.terminals.create(sessionId, { idempotencyKey: key });
    view(entry, r.terminal_id);
    select(r.terminal_id);
    refreshList();
  });
  const close = useAction(async (key, id: string) => {
    await api.terminals.close(sessionId, id, { idempotencyKey: key });
    entry.views.delete(id);
    if (entry.selected === id) select(null);
    refreshList();
  });
  const input = useAction(async (key, data: string) => {
    await api.terminals.input(sessionId, selected!, data, { idempotencyKey: key });
  });

  const current = selected ? view(entry, selected) : null;
  const closed = current?.closed ?? false;

  useEffect(() => {
    if (!selected || sessionClosed) return;
    const v = view(entry, selected);
    if (v.closed) return;
    let stop = false;
    let busy = false;
    const tick = async () => {
      if (busy || v.closed) return;
      busy = true;
      try {
        const r = await api.terminals.output(sessionId, selected, v.offset);
        if (stop) return;
        setPollError(null);
        if (r.truncated && v.offset > 0) v.screen.feed(`\r\n${t("terminal.output_dropped")}\r\n`);
        if (r.data) v.screen.feed(r.data);
        v.offset = r.offset;
        if (r.closed) v.closed = true;
        if (r.data || r.closed) setVersion((n) => n + 1);
      } catch (e) {
        if (stop) return;
        if (isApiError(e) && e.code === "not_found") {
          // The runtime no longer holds it (exited and closed, or compute was replaced).
          entry.views.delete(selected);
          select(null);
          refreshList();
        } else {
          setPollError(e);
        }
      } finally {
        busy = false;
      }
    };
    void tick();
    const id = setInterval(() => void tick(), pollMs);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [api, sessionId, selected, sessionClosed, pollMs, entry, select, t]);

  // A lost response leaves the command's fate unknown: never resend it silently. The
  // same Idempotency-Key is kept, so "Retry" runs it at most once in total.
  const unknownOutcome = isApiError(input.error) && input.error.code === "outcome_unknown";
  const pendingCommand = unknownOutcome ? lastSent : null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (pendingCommand !== null || input.pending) return;
    const data = `${line}\n`;
    setLine("");
    setLastSent(data);
    await input.run(data);
  };
  const terminals = listed ?? (unlisted ? [...entry.views.keys()].map((id) => ({ terminal_id: id, closed: false })) : []);
  const unavailable = isApiError(list.error) && list.error.code === "executor_unavailable";

  if (sessionClosed) {
    return (
      <div className="term-tab">
        <div className="term-tab__start">
          {/* Terminals need a live lease; a closed Session can never host one again. */}
          <p className="faint" data-testid="terminal-closed">
            {t("session.compute_closed")}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="term-tab">
      {unavailable ? (
        <div className="notice notice--warn" role="status">
          <div className="notice__body">
            <div className="notice__title">{t("files.not_connected")}</div>
            <div className="notice__text">{t("terminal.unavailable_body")}</div>
          </div>
        </div>
      ) : !selected || !current ? (
        <div className="term-tab__start">
          {list.loading && !listed ? null : (
            <Button variant="primary" icon={<SquareTerminal size={14} />} disabled={start.pending} onClick={() => void start.run()}>
              {t("terminal.start")}
            </Button>
          )}
        </div>
      ) : (
        <>
          <div className="term-tab__bar">
            {terminals.length > 1 ? (
              <select
                className="select term-tab__pick"
                aria-label={t("terminal.pick")}
                value={selected}
                onChange={(e) => {
                  input.reset();
                  select(e.target.value);
                }}
              >
                {terminals.map((x, i) => (
                  <option key={x.terminal_id} value={x.terminal_id}>
                    {t("terminal.label", { n: i + 1 })} · {x.closed ? t("terminal.exited") : t("terminal.running")}
                  </option>
                ))}
              </select>
            ) : null}
            <Pill tone={closed ? "dim" : "ok"}>{t(closed ? "terminal.closed" : "terminal.connected")}</Pill>
            <span className="files__spacer" />
            <Button size="sm" variant="ghost" icon={<Plus size={12} />} disabled={start.pending} onClick={() => void start.run()}>
              {t("terminal.new")}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              icon={<X size={12} />}
              disabled={close.pending}
              title={t("terminal.close_hint")}
              onClick={() => void close.run(selected)}
            >
              {closed ? t("terminal.dismiss") : t("terminal.close")}
            </Button>
          </div>
          <ScreenView screen={current.screen} version={version} label={t("tab.terminal")} />
          {pendingCommand !== null ? (
            <div className="notice notice--warn" role="alert" data-testid="terminal-uncertain">
              <div className="notice__body">
                <div className="notice__title">{t("terminal.uncertain_title")}</div>
                <div className="notice__text">
                  {t("terminal.uncertain_body")} <code>{pendingCommand.trimEnd()}</code>
                </div>
              </div>
              <Button size="sm" variant="secondary" disabled={input.pending} onClick={() => void input.run(pendingCommand)}>
                {t("terminal.uncertain_retry")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => input.reset()}
              >
                {t("terminal.uncertain_discard")}
              </Button>
            </div>
          ) : null}
          <form className="term-tab__input" onSubmit={submit}>
            <span className="term-tab__prompt" aria-hidden="true">
              ❯
            </span>
            <Field label={t("terminal.input")} htmlFor="term-input">
              <Input
                id="term-input"
                className="mono"
                autoComplete="off"
                spellCheck={false}
                value={line}
                disabled={closed}
                onChange={(e) => setLine(e.target.value)}
              />
            </Field>
            <Button type="submit" size="sm" variant="secondary" disabled={closed || input.pending || pendingCommand !== null}>
              {t("terminal.send")}
            </Button>
          </form>
        </>
      )}
      <ErrorNotice error={start.error ?? close.error ?? (unknownOutcome ? null : input.error) ?? pollError ?? (unavailable || unlisted ? null : list.error)} />
    </div>
  );
}

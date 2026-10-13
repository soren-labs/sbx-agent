import { useEffect, useRef, useState, type FormEvent } from "react";
import { SquareTerminal } from "lucide-react";
import { useI18n } from "../../../i18n";
import { useApi } from "../../../state/context";
import { Button } from "../../../ui/Button";
import { ErrorNotice } from "../../../ui/ErrorNotice";
import { Field, Input } from "../../../ui/Field";
import { Pill } from "../../../ui/Pill";
import { useAction } from "../../../ui/useAction";

const MAX_BUFFER = 100_000;

/** Lease-local PTY: bounded in-memory buffer, polled output, no restored-process claims. */
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
  const [terminal, setTerminal] = useState<string | null>(null);
  const [buffer, setBuffer] = useState("");
  const [closed, setClosed] = useState(false);
  const [pollError, setPollError] = useState<unknown>(null);
  const [line, setLine] = useState("");
  const offset = useRef(0);

  const start = useAction(async (key) => {
    const r = await api.terminals.create(sessionId, { idempotencyKey: key });
    offset.current = 0;
    setBuffer("");
    setClosed(false);
    setTerminal(r.terminal_id);
  });
  const input = useAction(async (key, data: string) => {
    await api.terminals.input(sessionId, terminal!, data, { idempotencyKey: key });
  });

  useEffect(() => {
    if (!terminal || closed) return;
    let stop = false;
    const tick = async () => {
      try {
        const r = await api.terminals.output(sessionId, terminal, offset.current);
        if (stop) return;
        setPollError(null);
        offset.current = r.offset;
        if (r.data) setBuffer((b) => (b + r.data).slice(-MAX_BUFFER));
        if (r.closed) setClosed(true);
      } catch (e) {
        if (!stop) setPollError(e);
      }
    };
    void tick();
    const id = setInterval(() => void tick(), pollMs);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [api, sessionId, terminal, closed, pollMs]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const data = `${line}\n`;
    setLine("");
    await input.run(data);
  };

  return (
    <div className="term-tab">
      {!terminal ? (
        <div className="term-tab__start">
          {/* Terminals need a live lease; a closed Session can never host one again. */}
          {sessionClosed ? (
            <p className="faint" data-testid="terminal-closed">
              {t("session.compute_closed")}
            </p>
          ) : (
            <Button variant="primary" icon={<SquareTerminal size={14} />} disabled={start.pending} onClick={() => void start.run()}>
              {t("terminal.start")}
            </Button>
          )}
        </div>
      ) : (
        <>
          <div className="term-tab__bar">
            <Pill tone={closed ? "dim" : "ok"}>{t(closed ? "terminal.closed" : "terminal.connected")}</Pill>
            <span className="files__spacer" />
            {closed && !sessionClosed ? (
              <Button size="sm" variant="secondary" disabled={start.pending} onClick={() => void start.run()}>
                {t("terminal.new")}
              </Button>
            ) : null}
          </div>
          <pre className="terminal" role="log" aria-label={t("tab.terminal")} tabIndex={0}>
            {buffer}
          </pre>
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
            <Button type="submit" size="sm" variant="secondary" disabled={closed || input.pending}>
              {t("terminal.send")}
            </Button>
          </form>
        </>
      )}
      <ErrorNotice error={start.error ?? input.error ?? pollError} />
    </div>
  );
}

import { useEffect, useRef, useState, type FormEvent } from "react";
import { ErrorNotice, Field, useAction } from "../../components/ui";
import { useI18n } from "../../i18n";
import { useApi } from "../../state/context";

const MAX_BUFFER = 100_000;

/** Lease-local PTY: bounded in-memory buffer, polled output, no restored-process claims. */
export function TerminalTab({ sessionId, pollMs = 1000 }: { sessionId: string; pollMs?: number }) {
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
    <div className="terminal-tab">
      {!terminal ? (
        <button type="button" className="btn btn-primary" disabled={start.pending} onClick={() => void start.run()}>
          {t("terminal.start")}
        </button>
      ) : (
        <>
          <pre className="terminal" role="log" aria-label={t("tab.terminal")} tabIndex={0}>
            {buffer}
          </pre>
          {closed ? <p className="muted small">{t("terminal.closed")}</p> : null}
          <form className="row" onSubmit={submit}>
            <Field id="term-input" label={t("terminal.input")}>
              <input id="term-input" className="mono" autoComplete="off" spellCheck={false} value={line} disabled={closed} onChange={(e) => setLine(e.target.value)} />
            </Field>
            <button type="submit" className="btn btn-sm" disabled={closed || input.pending}>
              {t("terminal.send")}
            </button>
          </form>
        </>
      )}
      <ErrorNotice error={start.error ?? input.error ?? pollError} />
    </div>
  );
}

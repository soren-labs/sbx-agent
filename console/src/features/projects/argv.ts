/**
 * Command syntax for Project checks and services.
 *
 * A command is split into an argv list with POSIX shell quoting and nothing else: no
 * variables, globs, pipes or redirection, and it never runs through a shell. The rules are
 * exactly Python's `shlex.split` (posix): whitespace separates words; '…' is literal;
 * inside "…" a backslash escapes only `"` and `\`; outside quotes a backslash escapes the
 * next character; '' or "" is an empty argument; quoted parts join their neighbours
 * (--msg="a b" is one word). `joinArgv` is `shlex.join`, so text round-trips.
 * tests/fixtures/argv_syntax.json holds the shared vectors.
 */

export type ArgvErrorCode = "unterminated_quote" | "trailing_backslash" | "empty_command" | "empty_program";

export class ArgvError extends Error {
  constructor(
    readonly code: ArgvErrorCode,
    readonly column: number | null = null,
  ) {
    super(code);
  }
}

const SPACE = /[ \t\r\n]/;

export function splitArgv(command: string): string[] {
  const words: string[] = [];
  let word = "";
  let inWord = false;
  let quote: "'" | '"' | null = null;
  let quoteAt = 0;
  for (let i = 0; i < command.length; i++) {
    const c = command[i];
    if (quote === "'") {
      if (c === "'") quote = null;
      else word += c;
    } else if (quote === '"') {
      if (c === '"') quote = null;
      else if (c === "\\" && i + 1 < command.length && (command[i + 1] === '"' || command[i + 1] === "\\")) word += command[++i];
      else word += c;
    } else if (c === "'" || c === '"') {
      quote = c;
      quoteAt = i;
      inWord = true;
    } else if (c === "\\") {
      if (i + 1 >= command.length) throw new ArgvError("trailing_backslash", i + 1);
      word += command[++i];
      inWord = true;
    } else if (SPACE.test(c)) {
      if (inWord) words.push(word);
      word = "";
      inWord = false;
    } else {
      word += c;
      inWord = true;
    }
  }
  if (quote) throw new ArgvError("unterminated_quote", quoteAt + 1);
  if (inWord) words.push(word);
  return words;
}

const SAFE = /^[A-Za-z0-9_@%+=:,./-]+$/;

/** One argument as text that `splitArgv` reads back unchanged (Python `shlex.quote`). */
export function quoteArg(arg: string): string {
  if (!arg) return "''";
  if (SAFE.test(arg)) return arg;
  return `'${arg.replace(/'/g, `'"'"'`)}'`;
}

export function joinArgv(argv: string[]): string {
  return argv.map(quoteArg).join(" ");
}

/** Unquoted words a shell would treat as operators; here they are plain arguments. */
const OPERATORS = new Set(["|", "||", "&&", "&", ";", ">", ">>", "<", "2>", "2>&1", "&>"]);

export interface ParsedCommand {
  name: string;
  argv: string[];
  /** Shell operators that were written unquoted and will be passed as literal arguments. */
  operators: string[];
}

const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

/**
 * `name=command` or a bare command (named after its program). The text before the first
 * `=` is a name only when it is a plain word, so `python3 -c "x=1"` is a bare command.
 */
export function parseCommandLine(line: string): ParsedCommand {
  const eq = line.indexOf("=");
  const head = eq > 0 ? line.slice(0, eq).trim() : "";
  const named = NAME.test(head);
  const command = named ? line.slice(eq + 1) : line;
  if (!command.trim()) throw new ArgvError("empty_command");
  let argv: string[];
  try {
    argv = splitArgv(command);
  } catch (e) {
    // Columns count from the start of the line the user typed, name included.
    if (e instanceof ArgvError && e.column !== null && named) throw new ArgvError(e.code, e.column + eq + 1);
    throw e;
  }
  if (!argv.length) throw new ArgvError("empty_command");
  if (!argv[0]) throw new ArgvError("empty_program");
  const operators = unquotedOperators(command);
  const name = named ? head : (argv[0].split("/").pop() ?? argv[0]);
  return { name, argv, operators };
}

/** Operators that appear as whole unquoted words (a quoted '&&' is deliberate). */
function unquotedOperators(command: string): string[] {
  const found: string[] = [];
  let raw = "";
  let quoted = false;
  let quote: string | null = null;
  const flush = () => {
    if (!quoted && OPERATORS.has(raw) && !found.includes(raw)) found.push(raw);
    raw = "";
    quoted = false;
  };
  for (let i = 0; i < command.length; i++) {
    const c = command[i];
    if (quote) {
      if (c === quote) quote = null;
      else if (quote === '"' && c === "\\") i++;
      continue;
    }
    if (c === "'" || c === '"') {
      quote = c;
      quoted = true;
    } else if (c === "\\") {
      quoted = true;
      i++;
    } else if (SPACE.test(c)) flush();
    else raw += c;
  }
  flush();
  return found;
}

export interface CommandLineResult {
  line: number;
  text: string;
  parsed: ParsedCommand | null;
  error: ArgvError | null;
}

/** Every non-blank line of a commands textarea, parsed independently. */
export function parseCommandLines(text: string): CommandLineResult[] {
  return text
    .split("\n")
    .map((t, i) => ({ text: t, line: i + 1 }))
    .filter((l) => l.text.trim())
    .map(({ text: t, line }) => {
      try {
        return { line, text: t, parsed: parseCommandLine(t.trim()), error: null };
      } catch (e) {
        if (e instanceof ArgvError) return { line, text: t, parsed: null, error: e };
        throw e;
      }
    });
}

export function commandLine(name: string, argv: string[]): string {
  return `${name}=${joinArgv(argv)}`;
}

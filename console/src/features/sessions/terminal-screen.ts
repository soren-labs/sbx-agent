/**
 * A streaming model of terminal output for the line-based command console.
 *
 * PTY output is fed in arbitrary chunks; escape sequences and characters may be split
 * between chunks, so the parser keeps its state across `feed` calls. Colours and text
 * attributes become styles, carriage returns and erase sequences overwrite text in place
 * (progress bars), wide characters take two columns, and every other control sequence
 * (bracketed paste, titles, cursor modes, …) is consumed instead of rendered as noise.
 * There is no fixed screen size: lines form a bounded scrollback.
 */

export interface Style {
  fg?: string;
  bg?: string;
  bold?: boolean;
  dim?: boolean;
  italic?: boolean;
  underline?: boolean;
  inverse?: boolean;
}

export interface Cell {
  ch: string;
  /** 1 or 2 columns; 0 marks the right half of a wide character. */
  w: number;
  style: Style;
}

export interface Run {
  text: string;
  style: Style;
}

const NONE: Style = {};
const BLANK: Cell = { ch: " ", w: 1, style: NONE };
export const MAX_LINES = 5000;

type Mode = "ground" | "esc" | "csi" | "string" | "string_esc" | "charset";

/** Wide (two-column) code points: East Asian wide/fullwidth and most emoji. */
function isWide(cp: number): boolean {
  return (
    (cp >= 0x1100 && cp <= 0x115f) ||
    (cp >= 0x2e80 && cp <= 0xa4cf && cp !== 0x303f) ||
    (cp >= 0xac00 && cp <= 0xd7a3) ||
    (cp >= 0xf900 && cp <= 0xfaff) ||
    (cp >= 0xfe30 && cp <= 0xfe4f) ||
    (cp >= 0xff00 && cp <= 0xff60) ||
    (cp >= 0xffe0 && cp <= 0xffe6) ||
    (cp >= 0x1f300 && cp <= 0x1f64f) ||
    (cp >= 0x1f900 && cp <= 0x1f9ff) ||
    (cp >= 0x20000 && cp <= 0x3fffd)
  );
}

/** Zero-width code points that attach to the previous character. */
function isZeroWidth(cp: number): boolean {
  return (
    (cp >= 0x0300 && cp <= 0x036f) ||
    (cp >= 0x200b && cp <= 0x200f) ||
    (cp >= 0xfe00 && cp <= 0xfe0f) ||
    (cp >= 0x20d0 && cp <= 0x20ff) ||
    cp === 0x200d
  );
}

export function charWidth(ch: string): number {
  const cp = ch.codePointAt(0) ?? 0;
  if (isZeroWidth(cp)) return 0;
  return isWide(cp) ? 2 : 1;
}

const BASIC = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"];

function color256(n: number): string | undefined {
  if (!Number.isInteger(n) || n < 0 || n > 255) return undefined;
  if (n < 8) return BASIC[n];
  if (n < 16) return `bright-${BASIC[n - 8]}`;
  if (n < 232) {
    const v = n - 16;
    const level = (x: number) => (x === 0 ? 0 : 55 + x * 40);
    return rgb(level(Math.floor(v / 36)), level(Math.floor(v / 6) % 6), level(v % 6));
  }
  const g = 8 + (n - 232) * 10;
  return rgb(g, g, g);
}

function rgb(r: number, g: number, b: number): string {
  const hex = (x: number) => Math.max(0, Math.min(255, x | 0)).toString(16).padStart(2, "0");
  return `#${hex(r)}${hex(g)}${hex(b)}`;
}

export class TerminalScreen {
  private lines: Cell[][] = [[]];
  private row = 0;
  private col = 0;
  private saved: [number, number] = [0, 0];
  private style: Style = NONE;
  private mode: Mode = "ground";
  private params = "";
  /** Lines dropped from the top of the scrollback so far. */
  dropped = 0;

  constructor(private readonly maxLines = MAX_LINES) {}

  feed(text: string): void {
    for (const ch of text) this.step(ch);
    this.trim();
  }

  private step(ch: string): void {
    switch (this.mode) {
      case "esc":
        return this.escape(ch);
      case "csi": {
        const c = ch.charCodeAt(0);
        if (c >= 0x40 && c <= 0x7e) {
          this.mode = "ground";
          this.csi(this.params, ch);
        } else if (c >= 0x20 && c <= 0x3f) {
          this.params += ch;
        } else {
          // A control character inside a CSI: abandon the sequence, keep the control.
          this.mode = "ground";
          this.control(ch);
        }
        return;
      }
      case "string":
        // OSC/DCS/APC/PM bodies end at BEL or ST (ESC \); their content is never shown.
        if (ch === "\x07") this.mode = "ground";
        else if (ch === "\x1b") this.mode = "string_esc";
        return;
      case "string_esc":
        this.mode = ch === "\x1b" ? "string_esc" : ch === "\\" ? "ground" : "string";
        return;
      case "charset":
        this.mode = "ground";
        return;
      default:
        if (ch === "\x1b") {
          this.mode = "esc";
          return;
        }
        if (ch < " " || ch === "\x7f") return this.control(ch);
        if (ch >= "\x80" && ch <= "\x9f") return; // C1 controls
        return this.print(ch);
    }
  }

  private escape(ch: string): void {
    this.mode = "ground";
    switch (ch) {
      case "[":
        this.mode = "csi";
        this.params = "";
        return;
      case "]":
      case "P":
      case "X":
      case "^":
      case "_":
        this.mode = "string";
        return;
      case "(":
      case ")":
      case "*":
      case "+":
        this.mode = "charset";
        return;
      case "7":
        this.saved = [this.row, this.col];
        return;
      case "8":
        [this.row, this.col] = this.saved;
        this.ensureRow();
        return;
      case "M":
        this.row = Math.max(0, this.row - 1);
        return;
      case "E":
        this.newline();
        return;
      case "c":
        this.reset();
        return;
      default:
        return; // ESC =, ESC >, ESC D and other single-character sequences
    }
  }

  private control(ch: string): void {
    switch (ch) {
      case "\n":
      case "\v":
      case "\f":
        return this.newline();
      case "\r":
        this.col = 0;
        return;
      case "\b":
        this.col = Math.max(0, this.col - 1);
        return;
      case "\t":
        this.col = (Math.floor(this.col / 8) + 1) * 8;
        return;
      default:
        return; // BEL, NUL, SO/SI and other C0 controls are not printable
    }
  }

  private newline(): void {
    this.row += 1;
    this.col = 0;
    this.ensureRow();
  }

  private ensureRow(): void {
    while (this.lines.length <= this.row) this.lines.push([]);
  }

  private line(): Cell[] {
    this.ensureRow();
    return this.lines[this.row];
  }

  /** Blank both halves of a wide character that a write at `at` would split. */
  private unsplit(line: Cell[], at: number): void {
    const cell = line[at];
    if (!cell) return;
    if (cell.w === 0 && at > 0) line[at - 1] = BLANK;
    if (cell.w === 2 && line[at + 1]?.w === 0) line[at + 1] = BLANK;
    line[at] = BLANK;
  }

  private print(ch: string): void {
    const width = charWidth(ch);
    const line = this.line();
    if (width === 0) {
      // Combining mark: attach to the character just written.
      let at = this.col - 1;
      while (at > 0 && line[at]?.w === 0) at--;
      if (at >= 0 && line[at]) line[at] = { ...line[at], ch: line[at].ch + ch };
      return;
    }
    while (line.length < this.col) line.push(BLANK);
    this.unsplit(line, this.col);
    if (width === 2) this.unsplit(line, this.col + 1);
    line[this.col] = { ch, w: width, style: this.style };
    if (width === 2) line[this.col + 1] = { ch: "", w: 0, style: this.style };
    this.col += width;
  }

  private csi(raw: string, final: string): void {
    const priv = /^[?<=>]/.test(raw);
    if (priv) return; // DEC private modes (bracketed paste, cursor visibility, …)
    const args = raw.split(";").map((p) => (p === "" ? NaN : Number(p)));
    const n = (i: number, d = 1) => (Number.isFinite(args[i]) && args[i] > 0 ? args[i] : d);
    const line = () => this.line();
    switch (final) {
      case "m":
        return this.sgr(raw === "" ? [0] : args.map((a) => (Number.isFinite(a) ? a : 0)));
      case "K": {
        const l = line();
        const mode = Number.isFinite(args[0]) ? args[0] : 0;
        if (mode === 0) {
          this.unsplit(l, this.col);
          l.splice(this.col);
        }
        else if (mode === 1) for (let i = 0; i <= this.col && i < l.length; i++) l[i] = BLANK;
        else if (mode === 2) l.length = 0;
        return;
      }
      case "J": {
        const mode = Number.isFinite(args[0]) ? args[0] : 0;
        if (mode === 2 || mode === 3) {
          this.dropped += this.lines.length;
          this.lines = [[]];
          this.row = 0;
          this.col = 0;
        } else if (mode === 0) {
          line().splice(this.col);
          this.lines.length = this.row + 1;
        }
        return;
      }
      case "G":
        this.col = n(0) - 1;
        return;
      case "C":
        this.col += n(0);
        return;
      case "D":
        this.col = Math.max(0, this.col - n(0));
        return;
      case "A":
        this.row = Math.max(0, this.row - n(0));
        return;
      case "B":
        this.row += n(0);
        this.ensureRow();
        return;
      case "E":
        this.row += n(0);
        this.col = 0;
        this.ensureRow();
        return;
      case "F":
        this.row = Math.max(0, this.row - n(0));
        this.col = 0;
        return;
      case "H":
      case "f":
        this.row = n(0) - 1;
        this.col = n(1) - 1;
        this.ensureRow();
        return;
      case "P": {
        const l = line();
        l.splice(this.col, n(0));
        return;
      }
      case "X": {
        const l = line();
        for (let i = this.col; i < this.col + n(0) && i < l.length; i++) l[i] = BLANK;
        return;
      }
      case "@": {
        const l = line();
        if (this.col < l.length) l.splice(this.col, 0, ...Array.from({ length: n(0) }, () => BLANK));
        return;
      }
      default:
        return; // scroll regions, device status reports, window ops: nothing to render
    }
  }

  private sgr(codes: number[]): void {
    let s: Style = { ...this.style };
    for (let i = 0; i < codes.length; i++) {
      const c = codes[i];
      if (c === 0) s = {};
      else if (c === 1) s.bold = true;
      else if (c === 2) s.dim = true;
      else if (c === 3) s.italic = true;
      else if (c === 4) s.underline = true;
      else if (c === 7) s.inverse = true;
      else if (c === 22) s.bold = s.dim = undefined;
      else if (c === 23) s.italic = undefined;
      else if (c === 24) s.underline = undefined;
      else if (c === 27) s.inverse = undefined;
      else if (c >= 30 && c <= 37) s.fg = BASIC[c - 30];
      else if (c === 39) s.fg = undefined;
      else if (c >= 40 && c <= 47) s.bg = BASIC[c - 40];
      else if (c === 49) s.bg = undefined;
      else if (c >= 90 && c <= 97) s.fg = `bright-${BASIC[c - 90]}`;
      else if (c >= 100 && c <= 107) s.bg = `bright-${BASIC[c - 100]}`;
      else if (c === 38 || c === 48) {
        let value: string | undefined;
        if (codes[i + 1] === 5) {
          value = color256(codes[i + 2]);
          i += 2;
        } else if (codes[i + 1] === 2) {
          value = rgb(codes[i + 2], codes[i + 3], codes[i + 4]);
          i += 4;
        }
        if (c === 38) s.fg = value;
        else s.bg = value;
      }
    }
    for (const k of Object.keys(s) as (keyof Style)[]) if (s[k] === undefined) delete s[k];
    this.style = Object.keys(s).length ? s : NONE;
  }

  private trim(): void {
    const over = this.lines.length - this.maxLines;
    if (over > 0) {
      this.lines.splice(0, over);
      this.row = Math.max(0, this.row - over);
      this.saved = [Math.max(0, this.saved[0] - over), this.saved[1]];
      this.dropped += over;
    }
  }

  private reset(): void {
    this.dropped += this.lines.length;
    this.lines = [[]];
    this.row = this.col = 0;
    this.style = NONE;
  }

  /** Lines as styled runs; a trailing empty line (the prompt's next row) is kept. */
  render(): Run[][] {
    return this.lines.map((line) => {
      const runs: Run[] = [];
      for (const cell of line) {
        if (cell.w === 0) continue;
        const last = runs[runs.length - 1];
        if (last && last.style === cell.style) last.text += cell.ch;
        else runs.push({ text: cell.ch, style: cell.style });
      }
      return runs;
    });
  }

  /** Plain text (tests, accessibility, copy). */
  text(): string {
    return this.render()
      .map((runs) => runs.map((r) => r.text).join("").replace(/\s+$/, ""))
      .join("\n");
  }
}

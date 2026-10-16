import { describe, expect, it } from "vitest";
import { TerminalScreen, charWidth } from "../features/sessions/terminal-screen";

const screen = (...chunks: string[]) => {
  const s = new TerminalScreen();
  for (const c of chunks) s.feed(c);
  return s;
};

describe("TerminalScreen (issue #227 AUD08)", () => {
  it("consumes bracketed paste, colours and titles instead of printing them", () => {
    const s = screen("\x1b[?2004h$ git status --short\r\n\x1b[31m M\x1b[m a.txt\r\n\x1b]0;user@host: ~\x07$ ");
    expect(s.text()).toBe("$ git status --short\n M a.txt\n$");
    expect(s.text()).not.toMatch(/\x1b|\[\?2004h|\[31m|\[m/);
    const runs = s.render()[1];
    expect(runs[0]).toEqual({ text: " M", style: { fg: "red" } });
    expect(runs[1].style).toEqual({});
  });

  it("keeps escape sequences split across chunks", () => {
    const s = screen("ok \x1b", "[3", "2m", "green\x1b[0", "m plain\x1b]0;ti", "tle\x1b", "\\ end");
    expect(s.text()).toBe("ok green plain end");
    expect(s.render()[0].find((r) => r.text === "green")?.style).toEqual({ fg: "green" });
  });

  it("overwrites in place on carriage return and erase-in-line (progress bars)", () => {
    const s = screen("progress 10%\r", "progress 55%\r", "progress 100%\x1b[K\r\ndone\r\n");
    expect(s.text()).toBe("progress 100%\ndone\n");
    expect(screen("abcdef\rxy\x1b[K").text()).toBe("xy");
    expect(screen("abcdef\rxy").text()).toBe("xycdef");
  });

  it("gives wide characters two columns and repairs split halves", () => {
    expect(charWidth("界")).toBe(2);
    expect(charWidth("a")).toBe(1);
    expect(charWidth("́")).toBe(0);
    // Overwriting the left half of a wide character blanks its right half.
    expect(screen("界面\rx").text()).toBe("x 面");
    // "\b" over a wide char moves one column: the next write splits it.
    expect(screen("a界\b\bZ").text()).toBe("aZ");
    expect(screen("é!").text()).toBe("é!");
  });

  it("handles 256-colour and true-colour SGR, resets and clear-screen", () => {
    const s = screen("\x1b[1;38;5;196mhot\x1b[22;48;2;0;0;255mblue\x1b[0m");
    const [hot, blue] = s.render()[0];
    expect(hot.style).toEqual({ bold: true, fg: "#ff0000" });
    expect(blue.style).toEqual({ fg: "#ff0000", bg: "#0000ff" });
    expect(screen("old\r\nlines\x1b[H\x1b[2Jfresh").text()).toBe("fresh");
  });

  it("bounds the scrollback", () => {
    const s = new TerminalScreen(10);
    s.feed(Array.from({ length: 25 }, (_, i) => `line ${i}\r\n`).join(""));
    const lines = s.text().split("\n");
    expect(lines.length).toBe(10);
    expect(lines[lines.length - 2]).toBe("line 24");
    expect(s.dropped).toBe(16);
  });
});

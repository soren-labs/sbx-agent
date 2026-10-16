import vectors from "../../../tests/fixtures/argv_syntax.json";
import { ArgvError, joinArgv, parseCommandLine, parseCommandLines, splitArgv } from "../features/projects/argv";

// The same vectors drive tests/integration/api/test_project_config.py, which checks them
// against Python's shlex and runs every case through a real declared check.
describe("check command syntax (AUD06)", () => {
  it.each(vectors.cases.map((c) => [c.line, c] as const))("%s", (_line, c) => {
    const parsed = parseCommandLine(c.line);
    expect(parsed.name).toBe(c.name);
    expect(parsed.argv).toEqual(c.argv);
    expect(parsed.operators).toEqual([]);
    // Text shown back in the editor reads back to the same argv.
    expect(splitArgv(joinArgv(c.argv))).toEqual(c.argv);
  });

  it("keeps the quoted program text for python -c, not a string literal", () => {
    expect(parseCommandLine('smoke=python3 -c "print(123)"').argv).toEqual(["python3", "-c", "print(123)"]);
  });

  it.each(vectors.errors.map((e) => [e.line, e.error] as const))("rejects %s", (line, code) => {
    let error: unknown = null;
    try {
      parseCommandLine(line);
    } catch (e) {
      error = e;
    }
    expect(error).toBeInstanceOf(ArgvError);
    expect((error as ArgvError).code).toBe(code);
  });

  it.each(vectors.operators.map((o) => [o.line, o] as const))("flags unquoted shell operators in %s", (_l, o) => {
    const parsed = parseCommandLine(o.line);
    expect(parsed.argv).toEqual(o.argv);
    expect(parsed.operators).toEqual(o.operators);
    if ("name" in o) expect(parsed.name).toBe(o.name);
  });

  it("joins like shlex.join", () => {
    expect(joinArgv(["python3", "-c", "print(123)"])).toBe("python3 -c 'print(123)'");
    expect(joinArgv(["a", "", "it's", "x y"])).toBe(`a '' 'it'"'"'s' 'x y'`);
  });

  it("parses each non-blank line and reports the line number of an error", () => {
    const out = parseCommandLines('a=echo 1\n\nb=python3 -c "oops\n  ');
    expect(out.map((l) => l.line)).toEqual([1, 3]);
    expect(out[0].parsed?.argv).toEqual(["echo", "1"]);
    expect(out[1].error?.code).toBe("unterminated_quote");
  });
});

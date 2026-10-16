import { describe, expect, it } from "vitest";
import { parseUnifiedDiff } from "../features/sessions/diff";

const PATCH = [
  "diff --git a/new.py b/new.py",
  "new file mode 100644",
  "index 0000000000000000000000000000000000000000..1111111111111111111111111111111111111111",
  "--- /dev/null",
  "+++ b/new.py",
  "@@ -0,0 +1,2 @@",
  "+print(1)",
  "+print(2)",
  "\\ No newline at end of file",
  "diff --git a/gone.txt b/gone.txt",
  "deleted file mode 100644",
  "index 2222222222222222222222222222222222222222..0000000000000000000000000000000000000000",
  "--- a/gone.txt",
  "+++ /dev/null",
  "@@ -1 +0,0 @@",
  "-bye",
  "diff --git a/src/app.ts b/src/app.ts",
  "index 3333333333333333333333333333333333333333..4444444444444444444444444444444444444444 100644",
  "--- a/src/app.ts",
  "+++ b/src/app.ts",
  "@@ -1,3 +1,3 @@",
  " one",
  "--- a separator line that was deleted",
  "+++ a separator line that was added",
  " three",
  "@@ -10,2 +10,3 @@ function tail()",
  " ten",
  "+ten and a half",
  "",
  "diff --git a/logo.png b/logo.png",
  "new file mode 100644",
  "index 0000000000000000000000000000000000000000..5555555555555555555555555555555555555555",
  "GIT binary patch",
  "literal 3",
  "KcmZ>A00001",
  "",
  "literal 0",
  "HcmV?d00001",
  "",
  "",
].join("\n");

describe("parseUnifiedDiff (issue #227 AUD19)", () => {
  const files = parseUnifiedDiff(PATCH);
  const byPath = new Map(files.map((f) => [f.path, f]));

  it("derives A/M/D from the patch, not from the manifest entry type", () => {
    expect(files.map((f) => [f.path, f.status])).toEqual([
      ["new.py", "new"],
      ["gone.txt", "deleted"],
      ["src/app.ts", "modified"],
      ["logo.png", "new"],
    ]);
    expect(byPath.get("logo.png")!.binary).toBe(true);
  });

  it("numbers only source lines: headers and no-newline markers have no numbers", () => {
    const added = byPath.get("new.py")!;
    const numbered = added.rows.filter((r) => r.old !== undefined || r.new !== undefined);
    expect(numbered.map((r) => [r.kind, r.new])).toEqual([
      ["add", 1],
      ["add", 2],
    ]);
    for (const r of added.rows.filter((r) => r.kind === "meta" || r.kind === "nonewline" || r.kind === "hunk")) {
      expect(r.old).toBeUndefined();
      expect(r.new).toBeUndefined();
    }
    expect(added.rows.find((r) => r.kind === "nonewline")?.text).toBe("\\ No newline at end of file");
  });

  it("counts by the hunk header, so lines starting with --- or +++ are code", () => {
    const app = byPath.get("src/app.ts")!;
    expect(app.add).toBe(2);
    expect(app.del).toBe(1);
    const rows = app.rows.filter((r) => r.kind !== "meta" && r.kind !== "hunk").map((r) => [r.kind, r.old ?? null, r.new ?? null]);
    expect(rows).toEqual([
      ["context", 1, 1],
      ["del", 2, null],
      ["add", null, 2],
      ["context", 3, 3],
      // second hunk restarts at its own header
      ["context", 10, 10],
      ["add", null, 11],
      // an empty line inside the hunk is context whose leading space was stripped
      ["context", 11, 12],
    ]);
  });

  it("never numbers the binary patch body or the trailing newline", () => {
    const png = byPath.get("logo.png")!;
    expect(png.rows.every((r) => r.kind === "meta")).toBe(true);
    expect(byPath.get("gone.txt")!.rows.filter((r) => r.kind === "del").map((r) => r.old)).toEqual([1]);
  });

  it("follows renames and paths with spaces", () => {
    const [renamed] = parseUnifiedDiff(
      ["diff --git a/old name.txt b/new name.txt", "similarity index 90%", "rename from old name.txt", "rename to new name.txt", "--- a/old name.txt", "+++ b/new name.txt", "@@ -1 +1 @@", "-a", "+b"].join("\n"),
    );
    expect(renamed).toMatchObject({ path: "new name.txt", oldPath: "old name.txt", status: "renamed", add: 1, del: 1 });
    const [spaced] = parseUnifiedDiff("diff --git a/a b/c.txt b/a b/c.txt\n--- a/a b/c.txt\n+++ b/a b/c.txt\n@@ -1 +1 @@\n-x\n+y\n");
    expect(spaced.path).toBe("a b/c.txt");
  });
});

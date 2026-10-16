/**
 * Unified diff (git) parser. Only lines inside a hunk are source lines: file headers,
 * extended headers, binary patches and "\ No newline at end of file" markers carry no
 * line numbers. Hunk bodies are consumed by the counts in their `@@` header, so a
 * deleted line that starts with "--" is still a deletion.
 */

export type DiffRowKind = "meta" | "hunk" | "context" | "add" | "del" | "nonewline";

export interface DiffRow {
  kind: DiffRowKind;
  text: string;
  old?: number;
  new?: number;
}

export type DiffStatus = "new" | "modified" | "deleted" | "renamed";

export interface DiffFile {
  /** Path after the change (before it, for a deletion). */
  path: string;
  oldPath: string | null;
  status: DiffStatus;
  binary: boolean;
  add: number;
  del: number;
  rows: DiffRow[];
}

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

/** `a/x` → `x`; `/dev/null` → null; strips git's quoting and a trailing tab. */
function headerPath(raw: string): string | null {
  let p = raw.replace(/\t.*$/, "");
  if (p.startsWith('"') && p.endsWith('"')) {
    try {
      p = JSON.parse(p) as string;
    } catch {
      p = p.slice(1, -1);
    }
  }
  if (p === "/dev/null") return null;
  return p.replace(/^[ab]\//, "");
}

/** Paths from `diff --git a/X b/Y` when X === Y (the common, unambiguous case). */
function gitHeaderPath(rest: string): string | null {
  const half = (rest.length - 1) / 2;
  if (Number.isInteger(half) && rest[half] === " ") {
    const a = rest.slice(0, half);
    const b = rest.slice(half + 1);
    if (a.startsWith("a/") && b.startsWith("b/") && a.slice(2) === b.slice(2)) return b.slice(2);
  }
  const m = rest.match(/^"?a\/(.+?)"? "?b\/(.+?)"?$/);
  return m ? m[2] : null;
}

export function parseUnifiedDiff(text: string): DiffFile[] {
  const lines = text.split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  const files: DiffFile[] = [];
  let file: DiffFile | null = null;
  let oldNo = 0;
  let newNo = 0;
  let oldLeft = 0;
  let newLeft = 0;
  let binaryBody = false;
  let lastInHunk = false;

  const start = (path: string): DiffFile => {
    const f: DiffFile = { path, oldPath: null, status: "modified", binary: false, add: 0, del: 0, rows: [] };
    files.push(f);
    return f;
  };

  for (const line of lines) {
    if (line.startsWith("diff --git ")) {
      file = start(gitHeaderPath(line.slice(11)) ?? "");
      file.rows.push({ kind: "meta", text: line });
      oldLeft = newLeft = 0;
      binaryBody = false;
      lastInHunk = false;
      continue;
    }
    if (!file) file = start("");
    const inHunk = oldLeft > 0 || newLeft > 0;
    if (inHunk && !binaryBody) {
      const tag = line[0];
      if (tag === "+") {
        file.rows.push({ kind: "add", text: line, new: newNo++ });
        file.add++;
        newLeft--;
      } else if (tag === "-") {
        file.rows.push({ kind: "del", text: line, old: oldNo++ });
        file.del++;
        oldLeft--;
      } else if (tag === "\\") {
        file.rows.push({ kind: "nonewline", text: line });
      } else {
        // " " context; an empty line is a context line whose space was stripped.
        file.rows.push({ kind: "context", text: line, old: oldNo++, new: newNo++ });
        oldLeft--;
        newLeft--;
      }
      lastInHunk = true;
      continue;
    }
    const hunk = binaryBody ? null : line.match(HUNK);
    if (hunk) {
      oldNo = Number(hunk[1]);
      newNo = Number(hunk[3]);
      oldLeft = hunk[2] === undefined ? 1 : Number(hunk[2]);
      newLeft = hunk[4] === undefined ? 1 : Number(hunk[4]);
      file.rows.push({ kind: "hunk", text: line });
      lastInHunk = false;
      continue;
    }
    if (line.startsWith("\\") && lastInHunk) {
      // The marker after a hunk's last line ("\ No newline at end of file").
      file.rows.push({ kind: "nonewline", text: line });
      continue;
    }
    lastInHunk = false;
    if (line.startsWith("new file mode")) file.status = "new";
    else if (line.startsWith("deleted file mode")) file.status = "deleted";
    else if (line.startsWith("rename from ")) {
      file.status = "renamed";
      file.oldPath = line.slice(12);
    } else if (line.startsWith("rename to ")) file.path = line.slice(10);
    else if (line.startsWith("--- ")) {
      const p = headerPath(line.slice(4));
      if (p === null) file.status = "new";
      else if (file.status === "renamed") file.oldPath = p;
      else if (!file.path) file.path = p;
    } else if (line.startsWith("+++ ")) {
      const p = headerPath(line.slice(4));
      if (p === null) file.status = "deleted";
      else file.path = p;
    } else if (line.startsWith("GIT binary patch") || /^Binary files .* differ$/.test(line)) {
      file.binary = true;
      binaryBody = line.startsWith("GIT binary patch");
    }
    file.rows.push({ kind: "meta", text: line });
  }
  return files.filter((f) => f.rows.length);
}

/**
 * Safe, minimal Markdown → DOM for agent messages. Builds nodes directly
 * (never innerHTML): fenced code, inline code, headings, lists, quotes,
 * bold/italic and http(s) links.
 */
import { h } from "./dom.js";

const SAFE_URL = /^(https?:|mailto:)/i;

function inline(text) {
  const out = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\([^)\s]+\))/g;
  let last = 0;
  let m = re.exec(text);
  while (m) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    if (m[1]) out.push(h("code", null, tok.slice(1, -1)));
    else if (m[2]) out.push(h("strong", null, tok.slice(2, -2)));
    else if (m[3]) out.push(h("em", null, tok.slice(1, -1)));
    else {
      const label = tok.slice(1, tok.indexOf("]"));
      const href = tok.slice(tok.indexOf("(") + 1, -1);
      out.push(
        SAFE_URL.test(href) ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, label) : tok,
      );
    }
    last = m.index + tok.length;
    m = re.exec(text);
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_SEP = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

function cells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

function table(head, rows) {
  return h(
    "div",
    { class: "md-table" },
    h(
      "table",
      null,
      h("thead", null, h("tr", null, cells(head).map((c) => h("th", null, inline(c))))),
      h("tbody", null, rows.map((r) => h("tr", null, cells(r).map((c) => h("td", null, inline(c)))))),
    ),
  );
}

export function renderMarkdown(src) {
  const root = h("div", { class: "md" });
  const lines = String(src ?? "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*```\s*([\w+-]*)\s*$/);
    if (fence) {
      const body = [];
      i += 1;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1;
      root.append(h("pre", { class: "md-code", dataset: { lang: fence[1] || null } }, h("code", null, body.join("\n"))));
      continue;
    }
    if (TABLE_ROW.test(line) && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const rows = [];
      i += 2;
      while (i < lines.length && TABLE_ROW.test(lines[i])) {
        rows.push(lines[i]);
        i += 1;
      }
      root.append(table(line, rows));
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      root.append(h(`h${Math.min(heading[1].length + 2, 6)}`, null, inline(heading[2])));
      i += 1;
      continue;
    }
    if (/^\s*[-*]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const list = h(ordered ? "ol" : "ul");
      while (i < lines.length && (ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*]\s+/).test(lines[i])) {
        list.append(h("li", null, inline(lines[i].replace(ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*]\s+/, ""))));
        i += 1;
      }
      root.append(list);
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      root.append(h("blockquote", null, inline(quote.join(" "))));
      continue;
    }
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const para = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*```/.test(lines[i]) &&
      !/^(#{1,4})\s+/.test(lines[i]) &&
      !/^\s*([-*]|\d+[.)])\s+/.test(lines[i]) &&
      !/^\s*>\s?/.test(lines[i]) &&
      !(TABLE_ROW.test(lines[i]) && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]))
    ) {
      para.push(lines[i]);
      i += 1;
    }
    const p = h("p");
    para.forEach((text, idx) => {
      if (idx) p.append(h("br"));
      p.append(...inline(text).map((n) => (typeof n === "string" ? document.createTextNode(n) : n)));
    });
    root.append(p);
  }
  return root;
}

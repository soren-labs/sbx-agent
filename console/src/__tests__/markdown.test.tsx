import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown } from "../prototype/Markdown";

describe("provider markdown", () => {
  it("preserves mixed Unicode, nested lists, code and GFM tables", () => {
    const code = "printf '你好 🌍'\n  echo a_b && echo '<tag>'";
    const { container } = render(<Markdown text={`## Result\n\n**Ready** with \`src/a_b.ts\`.\n\n1. First\n   - Nested\n2. Second\n\n> Quote\n\n- [x] Verified\n\n| File | State |\n| --- | --- |\n| a | done |\n\n\`\`\`sh\n${code}\n\`\`\``} />);
    expect(screen.getByRole("heading", {name:"Result"})).toBeVisible();
    expect(container.querySelector("ol ul li")).toHaveTextContent("Nested");
    expect(container.querySelector("pre code")?.textContent).toBe(code+"\n");
    expect(container.querySelector("table td")).toHaveTextContent("a");
    expect(screen.getByRole("checkbox")).toBeChecked();
    expect(screen.getByRole("checkbox")).toBeDisabled();
    expect(container.querySelector("blockquote")).toHaveTextContent("Quote");
  });
  it("keeps incomplete fenced code intact across stream updates", () => {
    const view = render(<Markdown text={'Text\n\n```ts\nconst emoji = "🌍'} />);
    expect(view.container.querySelector("pre code")).toHaveTextContent('const emoji = "🌍');
    view.rerender(<Markdown text={'Text\n\n```ts\nconst emoji = "🌍";\n```\n\nDone'} />);
    expect(view.container.querySelector("pre code")?.textContent).toBe('const emoji = "🌍";\n');
    expect(screen.getByText("Done")).toBeVisible();
    expect(view.container.querySelectorAll("pre")).toHaveLength(1);
  });
  it("blocks executable URLs and renders raw HTML as text", () => {
    const {container} = render(<Markdown text={'[bad](javascript:alert%281%29)\n\n<script>alert(1)</script>\n\n[docs](https://example.com/docs)'} />);
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("bad").getAttribute("href")).toBe("");
    expect(screen.getByText("docs")).toHaveAttribute("rel","noopener noreferrer");
  });
});

import { memo, type ComponentPropsWithoutRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const plugins = [remarkGfm];
// Render provider text as a syntax tree. Raw HTML stays text; links use the
// renderer's safe URL transform. Code is kept verbatim, including newlines.
function Link({ node: _node, ...props }: ComponentPropsWithoutRef<"a"> & { node?: unknown }) {
  return <a {...props} target="_blank" rel="noopener noreferrer" />;
}
export const Markdown = memo(function Markdown({ text, className = "assistant-message" }: {
  text: string;
  className?: string;
}) {
  return <div className={`${className} markdown-content`}>
    <ReactMarkdown remarkPlugins={plugins} components={{ a: Link }}>{text}</ReactMarkdown>
  </div>;
});

import type { ReactNode } from "react";

export type Tone = "ok" | "warn" | "err" | "run" | "info" | "dim";

export function Pill({ tone = "dim", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`pill pill--${tone}`} title={title}>
      {children}
    </span>
  );
}

export function StatusDot({ tone = "dim", className }: { tone?: Tone; className?: string }) {
  return <span className={`status-dot status-dot--${tone}${className ? ` ${className}` : ""}`} aria-hidden="true" />;
}

import type { ReactNode } from "react";

export function Spinner({ small }: { small?: boolean }) {
  return <span className={`spinner${small ? " spinner--sm" : ""}`} aria-hidden="true" />;
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

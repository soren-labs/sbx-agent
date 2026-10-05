import type { ReactNode } from "react";
import { Icon } from "../prototype/Icon";

export type Tone = "ok" | "warn" | "error" | "idle" | "busy";

export function Badge({ tone, children, role }: { tone: Tone; children: ReactNode; role?: string }) {
  return <span className={`hs-badge ${tone}`} role={role}><i aria-hidden="true" />{children}</span>;
}

export function ConnectionCard({ icon, title, description, badge, step, children, aside }: {
  icon: string; title: string; description: ReactNode; badge: ReactNode; step?: number; children?: ReactNode; aside?: ReactNode; label?: string;
}) {
  return <div className="hs-card-inner">
    <header className="hs-card-head">
      <span className="hs-card-icon"><Icon name={icon} size={18} />{step && <b>{step}</b>}</span>
      <div className="hs-card-title"><h2>{title}</h2><p>{description}</p></div>
      <div className="hs-card-badge">{badge}</div>
    </header>
    {aside}
    {children && <div className="hs-card-body">{children}</div>}
  </div>;
}

export const notifyConnectionChange = () => window.dispatchEvent(new Event("sbx-connection-change"));

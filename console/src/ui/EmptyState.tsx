import type { ReactNode } from "react";

export function EmptyState({
  icon,
  title,
  body,
  action,
}: {
  icon?: ReactNode;
  title: ReactNode;
  body?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      {icon ? <span className="empty-state__icon">{icon}</span> : null}
      <div className="empty-state__title">{title}</div>
      {body ? <div className="empty-state__body">{body}</div> : null}
      {action ? <div>{action}</div> : null}
    </div>
  );
}

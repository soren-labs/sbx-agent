import type { ReactNode } from "react";

/** Simple page header for list/settings pages; sticky under the shell top bar. */
export function PageHeader({ title, actions }: { title: ReactNode; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <h1 className="page-header__title truncate">{title}</h1>
      {actions ? <div className="page-header__actions">{actions}</div> : null}
    </header>
  );
}

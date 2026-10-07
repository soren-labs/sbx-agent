import { useI18n } from "../i18n";

export function Brand() {
  const { t } = useI18n();
  return (
    <div className="brand">
      <svg className="brand-mark" viewBox="0 0 32 32" fill="none" aria-hidden="true">
        <rect width="32" height="32" rx="9" fill="currentColor" />
        <path d="m10 10 6 6-6 6m8 0h5" stroke="var(--accent-fg)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span>{t("app.name")}</span>
    </div>
  );
}

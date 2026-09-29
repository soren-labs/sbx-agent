import { useEffect } from "react";
import { useI18n } from "../i18n";

/** "<page> · Session Console" while the page is mounted. */
export function useDocumentTitle(page: string) {
  const { t } = useI18n();
  useEffect(() => {
    document.title = page ? `${page} · ${t("app.name")}` : t("app.name");
  }, [page, t]);
}

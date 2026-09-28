import { Link } from "react-router-dom";
import { useI18n } from "../i18n";

export function NotFoundPage() {
  const { t } = useI18n();
  return (
    <div className="empty">
      <div className="e-icon"><span aria-hidden="true">⌕</span></div>
      <div>{t("common.not_found")}</div>
      <Link to="/">{t("common.back")}</Link>
    </div>
  );
}

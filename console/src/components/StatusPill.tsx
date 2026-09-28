import { useI18n } from "../i18n";
import type { SessionPhase, SessionEndReason } from "../api/types";

export function StatusPill({
  phase,
  endReason,
}: {
  phase: SessionPhase;
  endReason?: SessionEndReason;
}) {
  const { t } = useI18n();
  const label =
    phase === "ended" && endReason
      ? t(`end.${endReason}` as "end.cancelled")
      : t(`phase.${phase}` as "phase.idle");
  return (
    <span className={`pill pill-${phase}`} data-testid={`pill-${phase}`}>
      <span className="dot" />
      {label}
    </span>
  );
}

const PROVIDER_COLORS: Record<string, string> = {
  codex: "#10a37f",
  antigravity: "#6e56cf",
  grok: "#1d9bf0",
  opencode: "#f78166",
  devin: "#5b8af0",
};

export function ProviderBadge({
  provider,
  model,
}: {
  provider: string | null;
  model?: string | null;
}) {
  const { t } = useI18n();
  const name = provider ?? t("composer.auto");
  return (
    <span className="provider-badge" data-testid="provider-badge">
      <span
        className="swatch"
        style={{ background: PROVIDER_COLORS[name] ?? "var(--fg-faint)" }}
      />
      {name}
      {model ? <span className="faint">/ {model}</span> : null}
    </span>
  );
}

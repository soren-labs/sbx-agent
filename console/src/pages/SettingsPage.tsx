import { useState } from "react";
import type { DeliveryMode, EffortLevel } from "../api/types";
import { getToken, setToken } from "../api/http";
import { useI18n, type Locale } from "../i18n";
import { useTheme, type Theme } from "../theme";
import { loadDefaults, saveDefaults, type SessionDefaults } from "../state/prefs";
import { useDocumentTitle } from "../state/title";

export function SettingsPage() {
  const { t, locale, setLocale } = useI18n();
  useDocumentTitle(t("nav.settings"));
  const { theme, setTheme } = useTheme();
  const [defaults, setDefaultsState] = useState<SessionDefaults>(loadDefaults);
  const [token, setTokenState] = useState(getToken());
  const [saved, setSaved] = useState(false);

  const mode =
    (import.meta.env.VITE_API_MODE as string | undefined) ??
    (import.meta.env.VITE_API_BASE ? "http" : "mock");

  const save = () => {
    saveDefaults(defaults);
    setToken(token.trim());
    setSaved(true);
    setTimeout(() => setSaved(false), 1800);
  };

  return (
    <div>
      <h1>{t("settings.heading")}</h1>

      <div className="settings-card card">
        <h2>{t("settings.appearance")}</h2>
        <div className="field">
          <label htmlFor="set-theme">{t("settings.theme")}</label>
          <select
            id="set-theme"
            value={theme}
            onChange={(e) => setTheme(e.target.value as Theme)}
          >
            <option value="system">{t("settings.theme.system")}</option>
            <option value="light">{t("settings.theme.light")}</option>
            <option value="dark">{t("settings.theme.dark")}</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="set-lang">{t("settings.language")}</label>
          <select
            id="set-lang"
            value={locale}
            onChange={(e) => setLocale(e.target.value as Locale)}
          >
            <option value="en">English</option>
            <option value="zh-CN">简体中文</option>
          </select>
        </div>
      </div>

      <div className="settings-card card">
        <h2>{t("settings.connection")}</h2>
        <div className="field">
          <label>{t("settings.api_mode")}</label>
          <div className="row">
            <span className="pill pill-idle">
              {mode === "http" ? t("settings.api_mode.http") : t("settings.api_mode.mock")}
            </span>
          </div>
          <span className="hint">VITE_API_MODE / VITE_API_BASE</span>
        </div>
        <div className="field">
          <label htmlFor="set-token">{t("settings.api_token")}</label>
          <input
            id="set-token"
            type="password"
            autoComplete="off"
            value={token}
            onChange={(e) => setTokenState(e.target.value)}
            placeholder={t("settings.api_token_ph")}
          />
          <span className="hint">{t("settings.api_token_hint")}</span>
        </div>
      </div>

      <div className="settings-card card">
        <h2>{t("settings.defaults")}</h2>
        <div className="field">
          <label htmlFor="set-effort">{t("settings.effort_default")}</label>
          <select
            id="set-effort"
            value={defaults.effort}
            onChange={(e) =>
              setDefaultsState({
                ...defaults,
                effort: e.target.value as EffortLevel | "auto",
              })
            }
          >
            <option value="auto">Auto</option>
            {["none", "minimal", "low", "medium", "high", "xhigh", "max"].map(
              (ef) => (
                <option key={ef} value={ef}>{ef}</option>
              ),
            )}
          </select>
        </div>
        <div className="field">
          <label htmlFor="set-idle">{t("settings.idle_default")}</label>
          <input
            id="set-idle"
            type="number"
            min={1}
            value={defaults.idleTimeoutS}
            onChange={(e) =>
              setDefaultsState({
                ...defaults,
                idleTimeoutS: e.target.value === "" ? "" : Number(e.target.value),
              })
            }
            placeholder="1800"
          />
        </div>
        <div className="field">
          <label htmlFor="set-delivery">{t("composer.delivery")}</label>
          <select
            id="set-delivery"
            value={defaults.delivery}
            onChange={(e) =>
              setDefaultsState({
                ...defaults,
                delivery: e.target.value as DeliveryMode,
              })
            }
          >
            <option value="none">{t("composer.delivery.none")}</option>
            <option value="branch">{t("composer.delivery.branch")}</option>
            <option value="pr">{t("composer.delivery.pr")}</option>
            <option value="draft_pr">{t("composer.delivery.draft_pr")}</option>
          </select>
        </div>
        <button className="btn btn-primary" onClick={save}>
          {saved ? t("settings.saved") : t("common.save")}
        </button>
      </div>
    </div>
  );
}

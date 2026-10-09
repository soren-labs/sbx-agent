import { Icon } from "../../components/icons";
import { useState, type FormEvent } from "react";
import type { CreatedApiKey } from "../../api/types";
import { Empty, ErrorNotice, Field, Loading, useAction, when } from "../../components/ui";
import { useI18n, type Locale } from "../../i18n";
import { useApi } from "../../state/context";
import { useQuery } from "../../state/query";
import { useQueryClient } from "../../state/context";
import { useDocumentTitle } from "../../state/title";
import { useTheme, type Theme } from "../../theme";

function PasswordForm() {
  const { t } = useI18n();
  const api = useApi();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [done, setDone] = useState(false);
  const change = useAction(async (key, cur: string, nw: string) => {
    await api.auth.changePassword(cur, nw, { idempotencyKey: key });
    setDone(true);
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const [c, n] = [current, next];
    setCurrent("");
    setNext(""); // write-only
    setDone(false);
    void change.run(c, n);
  };
  return (
    <form className="card" onSubmit={submit} aria-label={t("settings.password")}>
      <h2>{t("settings.password")}</h2>
      <Field id="pw-current" label={t("settings.current_password")}>
        <input id="pw-current" type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
      </Field>
      <Field id="pw-new" label={t("settings.new_password")} hint={t("auth.password_hint")}>
        <input id="pw-new" type="password" autoComplete="new-password" minLength={8} required value={next} onChange={(e) => setNext(e.target.value)} />
      </Field>
      <ErrorNotice error={change.error} />
      {done ? <p role="status">{t("settings.password_changed")}</p> : null}
      <button type="submit" className="btn btn-primary btn-sm" disabled={change.pending}>
        {t("settings.change_password")}
      </button>
    </form>
  );
}

function ApiKeys() {
  const { t } = useI18n();
  const api = useApi();
  const qc = useQueryClient();
  const q = useQuery(["api-keys"], () => api.apiKeys.list());
  const [name, setName] = useState("");
  const [created, setCreated] = useState<CreatedApiKey | null>(null);
  const create = useAction(async (key) => {
    const k = await api.apiKeys.create(name.trim(), { idempotencyKey: key });
    setCreated(k); // plaintext shown once; discarded on dismiss
    setName("");
    qc.invalidate(["api-keys"]);
  });
  const revoke = useAction(async (key, id: string) => {
    await api.apiKeys.revoke(id, { idempotencyKey: key });
    qc.invalidate(["api-keys"]);
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (name.trim()) void create.run();
  };
  const [copied, setCopied] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  const copy = async () => {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.key);
      setCopied(true);
    } catch {
      setCopied(false); // clipboard blocked: the key stays selectable below
    }
  };
  const active = (q.data?.items ?? []).filter((k) => !k.revoked_at);
  const revoked = (q.data?.items ?? []).filter((k) => k.revoked_at);
  return (
    <section className="card" aria-labelledby="keys-h">
      <h2 id="keys-h">{t("settings.api_keys")}</h2>
      <p className="muted small">{t("settings.api_keys_intro")}</p>
      {created ? (
        <div className="key-reveal" role="status" data-testid="key-reveal">
          <div className="n-title">{t("settings.key_once")}</div>
          <code className="key-once" tabIndex={0}>
            {created.key}
          </code>
          <div className="key-usage">
            <span className="faint small">{t("settings.key_usage")}</span>
            <code>Authorization: Bearer {created.prefix}…</code>
          </div>
          <div className="hs-actions">
            <button type="button" className="button primary" onClick={() => void copy()}>
              <Icon name={copied ? "check" : "copy"} size={13} />
              {copied ? t("settings.key_copied") : t("settings.key_copy")}
            </button>
            <button
              type="button"
              className="button"
              onClick={() => {
                setCreated(null);
                setCopied(false);
              }}
            >
              {t("settings.key_dismiss")}
            </button>
          </div>
        </div>
      ) : null}
      <form className="key-form" onSubmit={submit} aria-label={t("settings.create_key")}>
        <Field id="key-name" label={t("settings.key_name")}>
          <input
            id="key-name"
            value={name}
            placeholder={t("settings.key_name_ph")}
            onChange={(e) => setName(e.target.value)}
            maxLength={80}
          />
        </Field>
        <button type="submit" className="button primary" disabled={!name.trim() || create.pending}>
          {t("settings.create_key")}
        </button>
      </form>
      <ErrorNotice error={create.error ?? revoke.error} />
      {q.loading && !q.data ? <Loading /> : null}
      {q.data && !q.data.items.length ? <Empty>{t("settings.no_keys")}</Empty> : null}
      <ul className="key-list" aria-label={t("settings.api_keys")}>
        {[...active, ...revoked].map((k) => (
          <li key={k.id} className={k.revoked_at ? "is-revoked" : ""}>
            <span className="key-name">
              <strong>{k.name}</strong>
              <code>{k.prefix}…</code>
            </span>
            <span className="faint small">
              {k.revoked_at ? `${t("settings.revoked")} ${when(k.revoked_at)}` : when(k.created_at)}
            </span>
            {!k.revoked_at ? (
              confirming === k.id ? (
                <span className="key-actions">
                  <button
                    type="button"
                    className="button danger"
                    disabled={revoke.pending}
                    onClick={() => void revoke.run(k.id).then(() => setConfirming(null))}
                  >
                    {t("settings.revoke_confirm")}
                  </button>
                  <button type="button" className="button ghost" onClick={() => setConfirming(null)}>
                    {t("common.cancel")}
                  </button>
                </span>
              ) : (
                <button type="button" className="button ghost danger" onClick={() => setConfirming(k.id)}>
                  {t("settings.revoke")}
                </button>
              )
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

export function SettingsPage() {
  const { t, locale, setLocale } = useI18n();
  useDocumentTitle(t("nav.settings"));
  const { theme, setTheme } = useTheme();
  return (
    <div className="narrow">
      <h1>{t("nav.settings")}</h1>
      <p className="page-lead muted">{t("settings.intro")}</p>
      <section className="card">
        <h2>{t("settings.appearance")}</h2>
        <div className="controls">
          <Field id="set-lang" label={t("settings.language")}>
            <select id="set-lang" value={locale} onChange={(e) => setLocale(e.target.value as Locale)}>
              <option value="en">English</option>
              <option value="zh-CN">简体中文</option>
            </select>
          </Field>
          <Field id="set-theme" label={t("settings.theme")}>
            <select id="set-theme" value={theme} onChange={(e) => setTheme(e.target.value as Theme)}>
              <option value="system">{t("settings.theme_system")}</option>
              <option value="light">{t("settings.theme_light")}</option>
              <option value="dark">{t("settings.theme_dark")}</option>
            </select>
          </Field>
        </div>
      </section>
      <PasswordForm />
      <ApiKeys />
    </div>
  );
}

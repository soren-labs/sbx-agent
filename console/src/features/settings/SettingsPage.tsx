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
  return (
    <section className="card" aria-labelledby="keys-h">
      <h2 id="keys-h">{t("settings.api_keys")}</h2>
      {created ? (
        <div className="notice warn" role="status">
          <div className="grow">
            <div className="n-title">{t("settings.key_once")}</div>
            <code className="key-once">{created.key}</code>
            <div className="n-actions">
              <button type="button" className="btn btn-sm" onClick={() => setCreated(null)}>
                {t("settings.key_dismiss")}
              </button>
            </div>
          </div>
        </div>
      ) : null}
      <form className="row wrap" onSubmit={submit} aria-label={t("settings.create_key")}>
        <Field id="key-name" label={t("settings.key_name")}>
          <input id="key-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={80} />
        </Field>
        <button type="submit" className="btn btn-sm" disabled={!name.trim() || create.pending}>
          {t("settings.create_key")}
        </button>
      </form>
      <ErrorNotice error={create.error ?? revoke.error} />
      {q.loading && !q.data ? <Loading /> : null}
      {q.data && !q.data.items.length ? <Empty>{t("settings.no_keys")}</Empty> : null}
      <ul className="plain-list">
        {(q.data?.items ?? []).map((k) => (
          <li key={k.id} className="row wrap small">
            <strong>{k.name}</strong>
            <code>{k.prefix}…</code>
            <span className="faint grow">{k.revoked_at ? `${t("settings.revoked")} ${when(k.revoked_at)}` : when(k.created_at)}</span>
            {!k.revoked_at ? (
              <button type="button" className="btn btn-sm btn-danger" disabled={revoke.pending} onClick={() => void revoke.run(k.id)}>
                {t("settings.revoke")}
              </button>
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

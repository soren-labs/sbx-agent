import { useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { Check, Copy, KeyRound, Moon, Plus, Sun, SunMoon } from "lucide-react";
import type { ApiKey, ApiKeyScope, CreatedApiKey } from "../../api/types";
import { isApiError } from "../../api/errors";
import { randomKey } from "../../api/http";
import { useI18n, type Locale } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useApi, useQueryClient } from "../../state/context";
import { useAuth } from "../../state/auth";
import { useQuery } from "../../state/query";
import { useDocumentTitle } from "../../state/title";
import { useTheme, type Theme } from "../../theme";
import { Button } from "../../ui/Button";
import { Dialog } from "../../ui/Dialog";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Field, Input, Select } from "../../ui/Field";
import { when } from "../../ui/format";
import { Pill } from "../../ui/Pill";
import { Spinner } from "../../ui/Spinner";
import { Tabs } from "../../ui/Tabs";
import { useAction } from "../../ui/useAction";
import { PageHeader } from "../../shell/PageHeader";

type Tab = "general" | "account" | "keys";
const TABS: Tab[] = ["general", "account", "keys"];

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
    <section className="page__section" aria-label={t("settings.password")}>
      <div className="page__section-head">
        <h2>{t("settings.password")}</h2>
      </div>
      <form className="lcard" style={{ padding: "12px 16px", gap: 12 }} onSubmit={submit} aria-label={t("settings.password")}>
        <Field htmlFor="pw-current" label={t("settings.current_password")}>
          <Input
            id="pw-current"
            type="password"
            autoComplete="current-password"
            required
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </Field>
        <Field htmlFor="pw-new" label={t("settings.new_password")} hint={t("auth.password_hint")}>
          <Input
            id="pw-new"
            type="password"
            autoComplete="new-password"
            minLength={8}
            required
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
        </Field>
        <ErrorNotice error={change.error} />
        {done ? <p role="status">{t("settings.password_changed")}</p> : null}
        <div>
          <Button type="submit" variant="primary" size="sm" disabled={change.pending}>
            {t("settings.change_password")}
          </Button>
        </div>
      </form>
    </section>
  );
}

function GeneralTab() {
  const { t, locale, setLocale } = useI18n();
  const { theme, setTheme } = useTheme();
  const themes: { id: Theme; icon: React.ReactNode; label: string; hint: string }[] = [
    { id: "dark", icon: <Moon size={14} />, label: t("settings.theme_dark"), hint: t("settings.theme_dark_hint") },
    { id: "light", icon: <Sun size={14} />, label: t("settings.theme_light"), hint: t("settings.theme_light_hint") },
    { id: "system", icon: <SunMoon size={14} />, label: t("settings.theme_system"), hint: t("settings.theme_system_hint") },
  ];
  return (
    <>
      <section className="page__section" aria-label={t("settings.appearance")}>
        <div className="page__section-head">
          <h2>{t("settings.appearance")}</h2>
        </div>
        <div className="lcard">
          <div className="theme-cards" role="radiogroup" aria-label={t("settings.theme")}>
            {themes.map((th) => (
              <button
                key={th.id}
                type="button"
                role="radio"
                aria-checked={theme === th.id}
                className="theme-card"
                onClick={() => setTheme(th.id)}
              >
                <span className={`theme-card__preview theme-card__preview--${th.id}`} aria-hidden="true">
                  <i style={{ width: "30%", height: 8 }} />
                  <i style={{ width: "50%", height: 8 }} />
                </span>
                <span>
                  <strong>
                    {th.icon} {th.label}
                  </strong>
                  <br />
                  <span className="faint small">{th.hint}</span>
                </span>
              </button>
            ))}
          </div>
          <div className="lrow">
            <div className="lrow__line">
              <div className="lrow__main">
                <Field htmlFor="set-lang" label={t("settings.language")}>
                  <Select id="set-lang" value={locale} onChange={(e) => setLocale(e.target.value as Locale)} style={{ maxWidth: 240 }}>
                    <option value="en">English</option>
                    <option value="zh-CN">简体中文</option>
                  </Select>
                </Field>
              </div>
            </div>
          </div>
        </div>
      </section>
    </>
  );
}

function AccountTab() {
  const { t } = useI18n();
  const { me } = useAuth();
  return (
    <>
      <section className="page__section" aria-label={t("settings.account")}>
        <div className="page__section-head">
          <h2>{t("settings.account")}</h2>
        </div>
        <div className="lcard">
          <div className="lrow">
            <div className="lrow__line">
              <div className="lrow__main">
                <span className="lrow__sub">{t("settings.email")}</span>
                <span className="lrow__name">{me?.user.email}</span>
              </div>
              <Pill tone={me?.user.email_verified ? "ok" : "warn"}>
                {me?.user.email_verified ? (
                  <>
                    <Check size={12} /> {t("settings.verified")}
                  </>
                ) : (
                  t("settings.unverified")
                )}
              </Pill>
            </div>
          </div>
        </div>
      </section>
      <PasswordForm />
    </>
  );
}

const SCOPES: { id: ApiKeyScope; label: I18nKey; hint: I18nKey }[] = [
  { id: "read", label: "settings.scope.read", hint: "settings.scope.read_hint" },
  { id: "write", label: "settings.scope.write", hint: "settings.scope.write_hint" },
  { id: "*", label: "settings.scope.full", hint: "settings.scope.full_hint" },
];

/** The broadest scope a key holds, as a label ("*" > write > read). */
function scopeOf(scopes: string[]): (typeof SCOPES)[number] | null {
  for (const id of ["*", "write", "read"] as const) if (scopes.includes(id)) return SCOPES.find((s) => s.id === id)!;
  return null;
}

function ScopePill({ scopes }: { scopes: string[] }) {
  const { t } = useI18n();
  const s = scopeOf(scopes);
  return (
    <Pill tone={s?.id === "*" ? "warn" : "dim"} title={scopes.join(", ")}>
      {s ? t(s.label) : scopes.join(", ")}
    </Pill>
  );
}

/** Network failure or gateway 502-504 after retries: the key may have been minted. */
const uncertain = (e: unknown) =>
  isApiError(e) && (e.code === "outcome_unknown" || e.status === 0 || [502, 503, 504].includes(e.status));

function ApiKeysTab() {
  const { t } = useI18n();
  const api = useApi();
  const qc = useQueryClient();
  const q = useQuery(["api-keys"], () => api.apiKeys.list());
  const [name, setName] = useState("");
  const [scope, setScope] = useState<ApiKeyScope>("write");
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<CreatedApiKey | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // One Idempotency-Key per create request, kept while its outcome is unknown: retrying it
  // returns the key the server already minted instead of minting a second one.
  const issuance = useRef<string | null>(null);
  const [issuing, setIssuing] = useState(false);
  const [createError, setCreateError] = useState<unknown>(null);
  const [orphan, setOrphan] = useState<ApiKey | null>(null);
  const [unconfirmed, setUnconfirmed] = useState<string | null>(null);
  const pendingOutcome = issuance.current !== null && uncertain(createError);

  const create = async () => {
    const key = (issuance.current ??= randomKey());
    setIssuing(true);
    setCreateError(null);
    try {
      const k = await api.apiKeys.create(name.trim(), [scope], { idempotencyKey: key });
      issuance.current = null;
      setCreated(k); // plaintext shown once; discarded on dismiss
      setCopied(false);
      setName("");
      setScope("write");
      setCreating(false);
      setUnconfirmed(null);
    } catch (e) {
      if (!uncertain(e)) issuance.current = null;
      if (isApiError(e) && e.code === "secret_unavailable") {
        setOrphan(((e.details ?? {}) as { api_key?: ApiKey }).api_key ?? null);
        setCreating(false);
      } else setCreateError(e);
    } finally {
      setIssuing(false);
      qc.invalidate(["api-keys"]);
    }
  };
  const closeCreate = () => {
    // Leaving while the outcome is unknown: say so where the list is.
    if (pendingOutcome) setUnconfirmed(name.trim());
    issuance.current = null;
    setCreateError(null);
    setCreating(false);
  };
  const revoke = useAction(async (key, id: string) => {
    await api.apiKeys.revoke(id, { idempotencyKey: key });
    setConfirming(null);
    qc.invalidate(["api-keys"]);
  });
  const reissue = useAction(async (key, old: ApiKey) => {
    await api.apiKeys.revoke(old.id, { idempotencyKey: key });
    setOrphan(null);
    setName(old.name);
    setScope((scopeOf(old.scopes)?.id ?? "write") as ApiKeyScope);
    setCreating(true);
    qc.invalidate(["api-keys"]);
  });
  const copy = async () => {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.key);
      setCopied(true);
    } catch {
      setCopied(false); // clipboard blocked: the key stays selectable below
    }
  };
  const keys = q.data?.items ?? [];
  const active = keys.filter((k) => !k.revoked_at);
  const revoked = keys.filter((k) => k.revoked_at);
  const createdScope = created ? scopeOf(created.scopes) : null;

  return (
    <section className="page__section" aria-label={t("settings.api_keys")}>
      <div className="page__section-head">
        <h2>{t("settings.api_keys")}</h2>
        <p>{t("settings.api_keys_intro")}</p>
      </div>

      {created ? (
        <div className="key-reveal" role="status" data-testid="key-reveal">
          <div className="n-title">
            <strong>{t("settings.key_once")}</strong>
            <span className="faint small"> {t("settings.key_once_warn")}</span>
          </div>
          <code className="key-once" tabIndex={0}>
            {created.key}
          </code>
          <div className="key-usage">
            <span className="faint small">{t("settings.key_usage")}</span>
            <code>Authorization: Bearer {created.prefix}…</code>
          </div>
          {createdScope ? (
            <p className="faint small key-scope-line">
              {t("settings.key_access", { scope: t(createdScope.label) })} {t(createdScope.hint)}
            </p>
          ) : null}
          <div className="mc-actions">
            <Button variant="primary" size="sm" icon={<Copy size={13} />} onClick={() => void copy()}>
              {copied ? t("settings.key_copied") : t("settings.key_copy")}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                setCreated(null);
                setCopied(false);
              }}
            >
              {t("settings.key_dismiss")}
            </Button>
          </div>
        </div>
      ) : null}

      {orphan ? (
        <div className="notice notice--warn" role="alert" data-testid="key-orphan">
          <div className="notice__body">
            <div className="notice__title">{t("settings.key_orphan_title", { name: orphan.name })}</div>
            <div className="notice__text">{t(orphan.revoked_at ? "settings.key_orphan_revoked" : "settings.key_orphan_body")}</div>
            <div className="mc-actions">
              {orphan.revoked_at ? (
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => {
                    setName(orphan.name);
                    setOrphan(null);
                    setCreating(true);
                  }}
                >
                  {t("settings.create_key")}
                </Button>
              ) : (
                <Button size="sm" variant="primary" disabled={reissue.pending} onClick={() => void reissue.run(orphan)}>
                  {t("settings.key_orphan_reissue")}
                </Button>
              )}
              <Button size="sm" variant="ghost" onClick={() => setOrphan(null)}>
                {t("common.cancel")}
              </Button>
            </div>
            <ErrorNotice error={reissue.error} />
          </div>
        </div>
      ) : null}

      {unconfirmed !== null ? (
        <div className="notice notice--warn" role="status">
          <div className="notice__body">
            <div className="notice__title">{t("settings.key_unconfirmed_title")}</div>
            <div className="notice__text">{t("settings.key_unconfirmed_body", { name: unconfirmed })}</div>
            <div className="mc-actions">
              <Button size="sm" variant="ghost" onClick={() => setUnconfirmed(null)}>
                {t("settings.key_unconfirmed_dismiss")}
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      <ErrorNotice error={revoke.error} />
      {q.loading && !q.data ? <Spinner /> : null}
      {q.data && !keys.length ? (
        <div className="lcard">
          <div className="lrow__empty">
            {t("settings.no_keys")}
            <Button size="sm" variant="primary" icon={<Plus size={13} />} onClick={() => setCreating(true)}>
              {t("settings.create_key")}
            </Button>
          </div>
        </div>
      ) : null}
      {keys.length ? (
        <div className="lcard key-list" role="list" aria-label={t("settings.api_keys")}>
          {[...active, ...revoked].map((k) => (
            <div key={k.id} className="lrow" role="listitem" aria-label={k.name}>
              <div className="lrow__line">
                <span className="lrow__tile" aria-hidden="true">
                  <KeyRound size={15} />
                </span>
                <div className="lrow__main">
                  <span className="lrow__name">
                    {k.name} <ScopePill scopes={k.scopes} />
                  </span>
                  <span className="lrow__sub">
                    <code>{k.prefix}…</code>
                    {" · "}
                    {when(k.created_at)}
                  </span>
                </div>
                {k.revoked_at ? (
                  <span className="faint small">
                    {t("settings.revoked")} {when(k.revoked_at)}
                  </span>
                ) : (
                  <div className="lrow__aside">
                    <Button size="sm" variant="ghost" onClick={() => setConfirming(k.id)}>
                      {t("settings.revoke")}
                    </Button>
                  </div>
                )}
              </div>
            </div>
          ))}
          <div className="lrow__empty">
            <Button size="sm" variant="secondary" icon={<Plus size={13} />} onClick={() => setCreating(true)}>
              {t("settings.create_key")}
            </Button>
          </div>
        </div>
      ) : null}

      {creating ? (
        <Dialog title={t("settings.create_key")} onClose={closeCreate} width={480}>
          <form
            aria-label={t("settings.create_key")}
            onSubmit={(e) => {
              e.preventDefault();
              if (name.trim()) void create();
            }}
          >
            <Field htmlFor="key-name" label={t("settings.key_name")}>
              <Input
                id="key-name"
                value={name}
                placeholder={t("settings.key_name_ph")}
                onChange={(e) => setName(e.target.value)}
                maxLength={80}
                disabled={pendingOutcome}
              />
            </Field>
            <fieldset className="scope-picker" disabled={pendingOutcome}>
              <legend>{t("settings.key_access_label")}</legend>
              {SCOPES.map((s) => (
                <label key={s.id} className={s.id === scope ? "scope-picker__opt scope-picker__opt--on" : "scope-picker__opt"}>
                  <input type="radio" name="key-scope" value={s.id} checked={s.id === scope} onChange={() => setScope(s.id)} />
                  <span>
                    <strong>{t(s.label)}</strong>
                    <span className="faint small">{t(s.hint)}</span>
                  </span>
                </label>
              ))}
            </fieldset>
            {pendingOutcome ? (
              <div className="notice notice--warn" role="alert">
                <div className="notice__body">
                  <div className="notice__title">{t("settings.key_uncertain_title")}</div>
                  <div className="notice__text">{t("settings.key_uncertain_body")}</div>
                </div>
              </div>
            ) : (
              <ErrorNotice error={createError} />
            )}
            <div className="mc-actions">
              <Button type="submit" variant="primary" disabled={!name.trim() || issuing}>
                {pendingOutcome ? t("settings.key_retry_same") : t("settings.create_key")}
              </Button>
              <Button variant="ghost" onClick={closeCreate}>
                {t("common.cancel")}
              </Button>
            </div>
          </form>
        </Dialog>
      ) : null}

      {confirming ? (
        <Dialog
          title={t("settings.revoke_title")}
          onClose={() => setConfirming(null)}
          width={420}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirming(null)}>
                {t("common.cancel")}
              </Button>
              <Button
                variant="danger"
                disabled={revoke.pending}
                onClick={() => void revoke.run(confirming)}
              >
                {t("settings.revoke_confirm")}
              </Button>
            </>
          }
        >
          <p>{keys.find((k) => k.id === confirming)?.name}</p>
          <p className="faint small">{t("settings.revoke_body")}</p>
        </Dialog>
      ) : null}
    </section>
  );
}

export function SettingsPage() {
  const { t } = useI18n();
  useDocumentTitle(t("nav.settings"));
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab: Tab = TABS.includes(raw as Tab) ? (raw as Tab) : "general";
  const setTab = (id: string) => setParams(id === "general" ? {} : { tab: id }, { replace: true });

  return (
    <div className="page">
      <PageHeader title={t("nav.settings")} />
      <div className="settings-layout">
        <nav className="settings-nav" aria-label={t("settings.nav")}>
          {TABS.map((id) => (
            <button key={id} type="button" aria-current={tab === id} onClick={() => setTab(id)}>
              {t(`settings.tab.${id}` as I18nKey)}
            </button>
          ))}
        </nav>
        <div className="settings-nav--tabs">
          <Tabs
            label={t("settings.nav")}
            items={TABS.map((id) => ({ id, label: t(`settings.tab.${id}` as I18nKey) }))}
            value={tab}
            onChange={setTab}
          />
        </div>
        <div className="settings-body">
          {tab === "general" ? <GeneralTab /> : null}
          {tab === "account" ? <AccountTab /> : null}
          {tab === "keys" ? <ApiKeysTab /> : null}
        </div>
      </div>
    </div>
  );
}

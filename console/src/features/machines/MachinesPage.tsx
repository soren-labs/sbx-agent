import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type { MachineSlot, MachineSlotList, SlotStatus } from "../../api/types";
import { Icon, Spinner } from "../../components/icons";
import { ErrorNotice, Loading, useAction, when } from "../../components/ui";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useAuth } from "../../state/auth";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";
import { useDocumentTitle } from "../../state/title";
import "./machines.css";

const TONE: Record<SlotStatus, "ok" | "run" | "warn" | "err" | "dim"> = {
  ready: "ok",
  running: "run",
  login_pending: "warn",
  needs_login: "err",
  error: "err",
  deleting: "dim",
};
const STATUS_ORDER: SlotStatus[] = ["running", "login_pending", "needs_login", "error", "ready", "deleting"];
type Filter = "all" | "ready" | "running" | "login_pending" | "attention";
type Sort = "name" | "status" | "recent";
const COLLAPSED_KEY = "sbx.machines.collapsed";
/** Server reason codes with a plain-language explanation; unknown codes are shown as-is. */
const REASONS: Record<string, I18nKey> = {
  login_denied: "machines.reason.login_denied",
  login_failed: "machines.reason.login_failed",
  code_expired: "machines.reason.code_expired",
  login_window_elapsed: "machines.reason.code_expired",
  expired: "machines.reason.code_expired",
  cancelled: "machines.reason.cancelled",
  not_logged_in: "machines.reason.not_logged_in",
  logged_out: "machines.reason.logged_out",
  setup_vm_lost: "machines.reason.setup_vm_lost",
  profile_sync_failed: "machines.reason.profile_sync_failed",
  provider_rejected_login: "machines.reason.provider_rejected_login",
  usage_limited: "machines.reason.usage_limited",
};

/** Slots of the workspace, followed while anything is changing on the server. */
export function useMachineSlots() {
  const api = useApi();
  const qc = useQueryClient();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const q = useQuery(w ? ["machine-slots", w] : null, () => api.slots.list(w!));
  const moving = (q.data?.items ?? []).some((s) => s.status === "login_pending" || s.status === "deleting");
  const busy = (q.data?.items ?? []).some((s) => s.status === "running");
  useEffect(() => {
    if (!w || (!moving && !busy)) return;
    const id = setInterval(() => void qc.fetch(["machine-slots", w], () => api.slots.list(w)), moving ? 2000 : 6000);
    return () => clearInterval(id);
  }, [moving, busy, w, qc, api]);
  return q;
}

function StatusPill({ slot }: { slot: MachineSlot }) {
  const { t } = useI18n();
  return (
    <span className={`mc-status tone-${TONE[slot.status] ?? "dim"}`} data-status={slot.status}>
      <i aria-hidden="true" />
      {t(`machines.status.${slot.status}` as I18nKey)}
    </span>
  );
}

function remaining(iso: string | null, now: number): string | null {
  if (!iso) return null;
  const left = Math.max(0, Math.floor((new Date(iso).getTime() - now) / 1000));
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}

/** The official login, live: real URL and one-time code, then automatic verification. */
function LoginPanel({ slot, onChanged }: { slot: MachineSlot; onChanged: () => void }) {
  const { t } = useI18n();
  const api = useApi();
  const login = slot.login!;
  const [now, setNow] = useState(() => Date.now());
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const cancel = useAction(async (key) => {
    await api.slots.cancelLogin(slot.id, { idempotencyKey: key });
    onChanged();
  });
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(login.user_code ?? "");
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };
  const left = remaining(login.code_expires_at, now);
  return (
    <div className="mc-login" role="group" aria-label={t("machines.login.title")} data-login-state={login.state}>
      {login.state === "starting" ? (
        <p className="mc-login-wait" role="status">
          <Spinner /> {t(login.mode === "verify" ? "machines.login.checking" : "machines.login.starting")}
        </p>
      ) : null}
      {login.state === "awaiting_user" ? (
        <>
          <ol className="mc-steps">
            <li>
              <span>{t("machines.login.step_open", { provider: slot.provider_name })}</span>
              <a
                className="button primary mc-open"
                href={login.verification_url ?? "#"}
                target="_blank"
                rel="noopener noreferrer"
              >
                <Icon name="external" size={13} />
                {t("machines.login.open")}
              </a>
              <span className="mc-url">{login.verification_url}</span>
            </li>
            <li>
              <span>{t("machines.login.step_code")}</span>
              <span className="mc-code-row">
                <code className="mc-code" data-testid="device-code" aria-label={t("machines.login.code")}>
                  {login.user_code}
                </code>
                <button type="button" className="button" onClick={() => void copy()}>
                  <Icon name={copied ? "check" : "copy"} size={13} />
                  {copied ? t("machines.login.copied") : t("machines.login.copy")}
                </button>
              </span>
            </li>
          </ol>
          <p className="mc-login-wait" role="status">
            <Spinner /> {t("machines.login.waiting")}
            {left ? <span className="mc-expiry"> · {t("machines.login.expires", { time: left })}</span> : null}
          </p>
        </>
      ) : null}
      {login.state === "verifying" ? (
        <p className="mc-login-wait" role="status">
          <Spinner /> {t("machines.login.verifying")}
        </p>
      ) : null}
      <div className="mc-actions">
        <button type="button" className="button" disabled={cancel.pending} onClick={() => void cancel.run()}>
          {t("machines.login.cancel")}
        </button>
      </div>
      <ErrorNotice error={cancel.error} />
    </div>
  );
}

function SlotRow({
  slot,
  open,
  onToggle,
  onChanged,
}: {
  slot: MachineSlot;
  open: boolean;
  onToggle: () => void;
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const api = useApi();
  const [renaming, setRenaming] = useState(false);
  const [label, setLabel] = useState(slot.label);
  const [alias, setAlias] = useState(slot.account_alias ?? "");
  const [deleting, setDeleting] = useState(false);
  const [confirm, setConfirm] = useState("");
  const pending = slot.status === "login_pending";
  const catalog = slot.capabilities.catalog;

  const act = useAction(async (key, what: "login" | "verify" | "logout") => {
    await api.slots[what](slot.id, { idempotencyKey: key });
    onChanged();
  });
  const rename = useAction(async (key) => {
    await api.slots.update(slot.id, { label: label.trim(), account_alias: alias.trim() || null }, { idempotencyKey: key });
    setRenaming(false);
    onChanged();
  });
  const remove = useAction(async (key) => {
    await api.slots.remove(slot.id, confirm, { idempotencyKey: key });
    setDeleting(false);
    onChanged();
  });
  const reason = slot.state_reason ?? slot.login?.error_code ?? null;
  const reasonKey = reason ? REASONS[reason.split(":")[0]] : undefined;
  const panel = `machine-panel-${slot.id}`;
  const row = useRef<HTMLLIElement>(null);
  // A login that needs the person is brought into view: with many machines it opens below the fold.
  useEffect(() => {
    if (open && pending) row.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }, [open, pending]);

  return (
    <li ref={row} className={`mc-row ${open ? "open" : ""}`} data-testid="machine-row" data-status={slot.status}>
      <button type="button" className="mc-row-head" aria-expanded={open} aria-controls={panel} onClick={onToggle}>
        <span className="mc-icon" aria-hidden="true">
          <Icon name="machine" size={16} />
        </span>
        <span className="mc-name">
          <strong>{slot.label}</strong>
          <small>
            {slot.provider_name}
            {slot.account_alias ? ` · ${slot.account_alias}` : ""}
          </small>
        </span>
        <StatusPill slot={slot} />
        <Icon name={open ? "up" : "down"} size={12} />
      </button>
      {open ? (
        <div className="mc-panel" id={panel}>
          {pending && slot.login ? <LoginPanel slot={slot} onChanged={onChanged} /> : null}
          {!pending && (slot.status === "needs_login" || slot.status === "error") ? (
            <p className="mc-note err" role="status">
              {reasonKey ? t(reasonKey) : t("machines.reason.generic", { reason: reason ?? slot.status })}
            </p>
          ) : null}
          {slot.status === "ready" && reasonKey ? <p className="mc-note warn">{t(reasonKey)}</p> : null}
          {slot.status === "running" ? (
            <p className="mc-note run" role="status">
              {t("machines.running_note")}{" "}
              {slot.worker ? <Link to={`/sessions/${slot.worker.session_id}`}>{t("machines.open_session")}</Link> : null}
            </p>
          ) : null}

          {renaming ? (
            <form
              className="mc-form"
              onSubmit={(e) => {
                e.preventDefault();
                if (label.trim()) void rename.run();
              }}
            >
              <label>
                {t("machines.name")}
                <input value={label} maxLength={80} onChange={(e) => setLabel(e.target.value)} autoFocus />
              </label>
              <label>
                {t("machines.alias")}
                <input value={alias} maxLength={80} placeholder={t("machines.alias_ph")} onChange={(e) => setAlias(e.target.value)} />
              </label>
              <div className="mc-actions">
                <button type="submit" className="button primary" disabled={rename.pending || !label.trim()}>
                  {t("machines.save")}
                </button>
                <button type="button" className="button" onClick={() => setRenaming(false)}>
                  {t("common.cancel")}
                </button>
              </div>
              <ErrorNotice error={rename.error} />
            </form>
          ) : (
            <dl className="mc-details">
              <div>
                <dt>{t("machines.detail.models")}</dt>
                <dd>
                  {catalog?.status === "ready"
                    ? t("machines.detail.models_ready", { count: catalog.models.length, source: catalog.source ?? "" })
                    : slot.status === "ready" || slot.status === "running"
                      ? t("machines.detail.models_unavailable")
                      : "—"}
                </dd>
              </div>
              <div>
                <dt>{t("machines.detail.verified")}</dt>
                <dd>{when(slot.verified_at)}</dd>
              </div>
              <div>
                <dt>{t("machines.detail.cli")}</dt>
                <dd>{slot.capabilities.cli_version ?? "—"}</dd>
              </div>
              <div>
                <dt>{t("machines.detail.volume")}</dt>
                <dd>
                  <code>{slot.volume.name}</code>
                </dd>
              </div>
            </dl>
          )}

          {!renaming && !deleting && slot.status !== "deleting" ? (
            <div className="mc-actions">
              {!pending && !slot.busy ? (
                <button type="button" className="button" disabled={act.pending} onClick={() => void act.run("login")}>
                  <Icon name="refresh" size={13} />
                  {t(slot.status === "ready" ? "machines.relogin" : "machines.login_now")}
                </button>
              ) : null}
              {slot.status === "ready" ? (
                <button type="button" className="button" disabled={act.pending} onClick={() => void act.run("verify")}>
                  {t("machines.verify")}
                </button>
              ) : null}
              <button type="button" className="button" onClick={() => setRenaming(true)}>
                {t("machines.rename")}
              </button>
              {slot.status === "ready" && slot.volume.managed ? (
                <button type="button" className="button" disabled={act.pending} onClick={() => void act.run("logout")}>
                  {t("machines.logout")}
                </button>
              ) : null}
              {!slot.busy ? (
                <button type="button" className="button danger" onClick={() => setDeleting(true)}>
                  {t("machines.delete")}
                </button>
              ) : null}
            </div>
          ) : null}
          {deleting ? (
            <form
              className="mc-form"
              onSubmit={(e) => {
                e.preventDefault();
                if (confirm === slot.label) void remove.run();
              }}
            >
              <p className="mc-note err">
                {t(slot.volume.managed ? "machines.delete_warn" : "machines.delete_warn_adopted", { name: slot.label })}
              </p>
              <label>
                {t("machines.delete_confirm", { name: slot.label })}
                <input value={confirm} onChange={(e) => setConfirm(e.target.value)} autoFocus />
              </label>
              <div className="mc-actions">
                <button type="submit" className="button danger" disabled={confirm !== slot.label || remove.pending}>
                  {t("machines.delete")}
                </button>
                <button type="button" className="button" onClick={() => setDeleting(false)}>
                  {t("common.cancel")}
                </button>
              </div>
              <ErrorNotice error={remove.error} />
            </form>
          ) : null}
          <ErrorNotice error={act.error} />
        </div>
      ) : null}
    </li>
  );
}

function matches(slot: MachineSlot, filter: Filter): boolean {
  if (filter === "all") return true;
  if (filter === "attention") return slot.status === "needs_login" || slot.status === "error";
  return slot.status === filter;
}

export function MachinesPage() {
  const { t } = useI18n();
  const api = useApi();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  useDocumentTitle(t("nav.machines"));
  const q = useMachineSlots();
  const data: MachineSlotList | undefined = q.data;
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<Sort>("name");
  const [openId, setOpenId] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? "[]") as string[];
    } catch {
      return [];
    }
  });
  const seen = useRef(false);

  const toggleGroup = (provider: string) => {
    setCollapsed((current) => {
      const next = current.includes(provider) ? current.filter((p) => p !== provider) : [...current, provider];
      localStorage.setItem(COLLAPSED_KEY, JSON.stringify(next));
      return next;
    });
  };

  const add = useAction(async (key, provider: string) => {
    const slot = await api.slots.create(w!, { provider }, { idempotencyKey: key });
    setOpenId(slot.id);
    // The new machine must not be hidden by a filter, a search or a collapsed group.
    setFilter("all");
    setSearch("");
    setCollapsed((current) => current.filter((p) => p !== provider));
    q.refetch();
  });

  // A login already in progress when the page opens is shown without hunting for it.
  useEffect(() => {
    if (seen.current || !data) return;
    seen.current = true;
    const pending = data.items.find((s) => s.status === "login_pending");
    if (pending) setOpenId(pending.id);
  }, [data]);

  const items = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const list = (data?.items ?? []).filter(
      (s) =>
        matches(s, filter) &&
        (!needle || `${s.label} ${s.account_alias ?? ""} ${s.provider_name}`.toLowerCase().includes(needle)),
    );
    return [...list].sort((a, b) =>
      sort === "status"
        ? STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status) || a.label.localeCompare(b.label)
        : sort === "recent"
          ? (b.last_used_at ?? b.created_at).localeCompare(a.last_used_at ?? a.created_at)
          : a.label.localeCompare(b.label, undefined, { numeric: true }),
    );
  }, [data, search, filter, sort]);

  const providers = data?.providers ?? [];
  const available = providers.filter((p) => p.available);
  const groups = providers
    .map((p) => ({ provider: p, slots: items.filter((s) => s.provider === p.provider_id) }))
    .filter((g) => g.slots.length > 0 || (data?.items ?? []).every((s) => s.provider !== g.provider.provider_id));
  const total = data?.summary.total ?? 0;

  return (
    <div className="hs-page settings-content mc-page">
      <div className="page-eyebrow">{t("machines.eyebrow")}</div>
      <div className="mc-title">
        <h1>{t("machines.title")}</h1>
        <button
          type="button"
          className="button primary"
          disabled={add.pending || available.length === 0 || !w}
          onClick={() => void add.run(available[0].provider_id)}
        >
          <Icon name="plus" size={13} />
          {t("machines.add", { provider: available[0]?.display_name ?? "Codex" })}
        </button>
      </div>
      <p className="hs-lead">{t("machines.lead")}</p>

      {q.loading && !data ? <Loading /> : null}
      <ErrorNotice error={q.error ?? add.error} onRetry={q.error ? q.refetch : undefined} />
      {data && available.length === 0 ? (
        <p className="mc-note warn" role="status">
          {t("machines.unavailable")} <Link to="/connections">{t("machines.connect_modal")}</Link>
        </p>
      ) : null}

      {data && total > 0 ? (
        <>
          <p className="mc-summary" data-testid="machine-summary" aria-live="polite">
            {t("machines.summary", {
              total,
              running: data.summary.running,
              ready: data.summary.ready,
              attention: data.summary.needs_attention + data.summary.login_pending,
            })}
          </p>
          <div className="mc-toolbar" role="search">
            <label className="mc-search">
              <Icon name="search" size={13} />
              <input
                type="search"
                aria-label={t("machines.search")}
                placeholder={t("machines.search")}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
            <select aria-label={t("machines.filter")} value={filter} onChange={(e) => setFilter(e.target.value as Filter)}>
              {(["all", "ready", "running", "login_pending", "attention"] as Filter[]).map((f) => (
                <option key={f} value={f}>
                  {t(`machines.filter.${f}` as I18nKey)}
                </option>
              ))}
            </select>
            <select aria-label={t("machines.sort")} value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
              {(["name", "status", "recent"] as Sort[]).map((s) => (
                <option key={s} value={s}>
                  {t(`machines.sort.${s}` as I18nKey)}
                </option>
              ))}
            </select>
          </div>
        </>
      ) : null}

      {data && total === 0 && available.length > 0 ? (
        <div className="mc-empty">
          <Icon name="machine" size={22} />
          <strong>{t("machines.empty_title")}</strong>
          <p>{t("machines.empty_body")}</p>
        </div>
      ) : null}

      {total > 0
        ? groups.map(({ provider, slots }) => {
            const all = (data?.items ?? []).filter((s) => s.provider === provider.provider_id);
            const running = all.filter((s) => s.status === "running").length;
            const isCollapsed = collapsed.includes(provider.provider_id);
            const body = `machine-group-${provider.provider_id}`;
            return (
              <section className="mc-group" key={provider.provider_id} aria-label={provider.display_name}>
                <h2>
                  <button
                    type="button"
                    className="mc-group-head"
                    aria-expanded={!isCollapsed}
                    aria-controls={body}
                    onClick={() => toggleGroup(provider.provider_id)}
                  >
                    <Icon name={isCollapsed ? "right" : "down"} size={12} />
                    <span>{provider.display_name}</span>
                    <small>{t("machines.group_count", { count: all.length, running })}</small>
                  </button>
                </h2>
                {!isCollapsed ? (
                  slots.length ? (
                    <ul className="mc-list" id={body}>
                      {slots.map((slot) => (
                        <SlotRow
                          key={slot.id}
                          slot={slot}
                          open={openId === slot.id}
                          onToggle={() => setOpenId(openId === slot.id ? null : slot.id)}
                          onChanged={q.refetch}
                        />
                      ))}
                    </ul>
                  ) : (
                    <p className="mc-note" id={body}>
                      {t("machines.no_match")}
                    </p>
                  )
                ) : null}
              </section>
            );
          })
        : null}
    </div>
  );
}

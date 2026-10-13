import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, Copy, Cpu, ExternalLink, MoreHorizontal, Plus, Search } from "lucide-react";
import type { MachineSlot, MachineSlotList, SlotStatus, SubscriptionProvider } from "../../api/types";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useAuth } from "../../state/auth";
import { useApi } from "../../state/context";
import { useDocumentTitle } from "../../state/title";
import { Button, IconButton } from "../../ui/Button";
import { Dialog } from "../../ui/Dialog";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { when } from "../../ui/format";
import { Menu, MenuItem } from "../../ui/Menu";
import { Pill, type Tone } from "../../ui/Pill";
import { Select } from "../../ui/Field";
import { Spinner } from "../../ui/Spinner";
import { useAction } from "../../ui/useAction";
import { PageHeader } from "../../shell/PageHeader";
import { useMachineSlots } from "./useMachineSlots";

const TONE: Record<SlotStatus, Tone> = {
  ready: "ok",
  running: "run",
  login_pending: "warn",
  needs_login: "warn",
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
  code_rejected: "machines.reason.code_rejected",
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

function StatusPill({ slot }: { slot: MachineSlot }) {
  const { t } = useI18n();
  return (
    <Pill tone={TONE[slot.status] ?? "dim"} title={slot.status}>
      <span data-status={slot.status}>{t(slot.status === "running" ? "machines.in_use" : (`machines.status.${slot.status}` as I18nKey))}</span>
    </Pill>
  );
}

/** Where a sign-in link goes, without its query: a provider may put a one-time code there. */
function destination(url: string | null): string {
  try {
    const { origin, pathname } = new URL(url ?? "");
    return origin + pathname;
  } catch {
    return "";
  }
}

function remaining(iso: string | null, now: number): string | null {
  if (!iso) return null;
  const left = Math.max(0, Math.floor((new Date(iso).getTime() - now) / 1000));
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}

/** Step 2 of a sign-in that hands back a code: paste what the browser showed, send it once. */
function PasteCode({ slot, onChanged }: { slot: MachineSlot; onChanged: () => void }) {
  const { t } = useI18n();
  const api = useApi();
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const send = useAction(async (key) => {
    await api.slots.submitLoginCode(slot.id, code.trim(), { idempotencyKey: key });
    // The code is used once; it is not kept in the page after it was sent.
    setCode("");
    setSent(true);
    onChanged();
  });
  // Until the next refresh shows the check, the field stays away: a second send is never typed.
  if (sent) {
    return (
      <p className="mc-login-wait" role="status">
        <Spinner small /> {t("machines.login.paste_sent")}
      </p>
    );
  }
  return (
    <form
      className="mc-paste"
      onSubmit={(e) => {
        e.preventDefault();
        if (code.trim()) void send.run();
      }}
    >
      <input
        className="mc-paste-input"
        data-testid="login-code-input"
        aria-label={t("machines.login.paste_label")}
        placeholder={t("machines.login.paste_placeholder")}
        autoComplete="off"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        value={code}
        onChange={(e) => setCode(e.target.value)}
      />
      <Button type="submit" variant="primary" size="sm" loading={send.pending} disabled={!code.trim()}>
        {t("machines.login.paste_send")}
      </Button>
      <ErrorNotice error={send.error} />
    </form>
  );
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
  const pasting = login.input === "authorization_code";
  return (
    <div className="mc-login" role="group" aria-label={t("machines.login.title")} data-login-state={login.state}>
      {login.state === "starting" ? (
        <p className="mc-login-wait" role="status">
          <Spinner small /> {t(login.mode === "verify" ? "machines.login.checking" : "machines.login.starting")}
        </p>
      ) : null}
      {login.state === "awaiting_user" ? (
        <>
          <ol className="mc-steps">
            <li>
              <span>{t("machines.login.step_open", { provider: slot.provider_name })}</span>
              <a
                className="btn btn--primary btn--sm mc-open"
                href={login.verification_url ?? "#"}
                target="_blank"
                rel="noopener noreferrer"
              >
                <ExternalLink size={13} />
                {t("machines.login.open")}
              </a>
              {/* The link is opened, not read: its query can be long or carry the one-time code. */}
              <span className="mc-url">{destination(login.verification_url)}</span>
            </li>
            {pasting ? (
              <li>
                <span>{t("machines.login.step_paste")}</span>
                <PasteCode slot={slot} onChanged={onChanged} />
              </li>
            ) : (
              <li>
                <span>{t("machines.login.step_code")}</span>
                <span className="mc-code-row">
                  <code className="mc-code" data-testid="device-code" aria-label={t("machines.login.code")}>
                    {login.user_code}
                  </code>
                  <Button size="sm" icon={<Copy size={13} />} onClick={() => void copy()}>
                    {copied ? t("machines.login.copied") : t("machines.login.copy")}
                  </Button>
                </span>
              </li>
            )}
          </ol>
          <p className="mc-login-wait" role="status">
            <Spinner small /> {t(pasting ? "machines.login.waiting_paste" : "machines.login.waiting")}
            {left ? (
              <span className="mc-expiry">
                {" "}
                · {t(pasting ? "machines.login.window" : "machines.login.expires", { time: left })}
              </span>
            ) : null}
          </p>
        </>
      ) : null}
      {login.state === "verifying" ? (
        <p className="mc-login-wait" role="status">
          <Spinner small />{" "}
          {t(login.method === "authorization_code" ? "machines.login.verifying_paste" : "machines.login.verifying")}
        </p>
      ) : null}
      <div className="mc-actions">
        <Button variant="secondary" disabled={cancel.pending} onClick={() => void cancel.run()}>
          {t("machines.login.cancel")}
        </Button>
      </div>
      <ErrorNotice error={cancel.error} />
    </div>
  );
}

function SlotRow({
  slot,
  open,
  loginOpen,
  onToggle,
  onChanged,
  onRename,
  onDelete,
  onSignIn,
}: {
  slot: MachineSlot;
  open: boolean;
  loginOpen: boolean;
  onToggle: () => void;
  onChanged: () => void;
  onRename: (slot: MachineSlot) => void;
  onDelete: (slot: MachineSlot) => void;
  onSignIn: (slot: MachineSlot) => void;
}) {
  const { t } = useI18n();
  const api = useApi();
  const pending = slot.status === "login_pending";
  const catalog = slot.capabilities.catalog;

  const act = useAction(async (key, what: "login" | "verify" | "logout") => {
    await api.slots[what](slot.id, { idempotencyKey: key });
    onChanged();
  });
  const reason = slot.state_reason ?? slot.login?.error_code ?? null;
  const reasonKey = reason ? REASONS[reason.split(":")[0]] : undefined;
  const panel = `machine-panel-${slot.id}`;
  const row = useRef<HTMLLIElement>(null);
  // A login that needs the person is brought into view: with many machines it opens below the fold.
  useEffect(() => {
    if (loginOpen && pending) row.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  }, [loginOpen, pending]);

  const catalogLine =
    catalog?.status === "ready"
      ? t("machines.catalog_summary", { count: catalog.models.length, source: catalog.source ?? "" })
      : slot.status === "ready" || slot.status === "running"
        ? t("machines.catalog_none")
        : null;

  return (
    <li ref={row} className="lrow mc-row" data-open={open} data-testid="machine-row" data-status={slot.status}>
      <div className="lrow__line">
        <button
          type="button"
          className="mc-row-head"
          aria-expanded={open}
          aria-controls={panel}
          onClick={onToggle}
        >
          <span className="lrow__tile" aria-hidden="true">
            <Cpu size={16} />
          </span>
          <span className="lrow__main">
            <span className="lrow__name">{slot.label}</span>
            <span className="lrow__sub">
              {slot.provider_name}
              {slot.account_alias ? ` · ${slot.account_alias}` : ""}
            </span>
            {catalogLine ? <span className="lrow__sub">{catalogLine}</span> : null}
          </span>
        </button>
        <StatusPill slot={slot} />
        <div className="lrow__aside">
          {slot.status === "needs_login" || slot.status === "error" ? (
            <Button size="sm" variant="primary" disabled={act.pending} onClick={() => void act.run("login").then(() => onSignIn(slot))}>
              {t("machines.sign_in")}
            </Button>
          ) : null}
          {pending ? (
            <Button size="sm" variant="secondary" onClick={() => onSignIn(slot)}>
              {t("machines.continue_login")}
            </Button>
          ) : null}
          <Menu
            align="end"
            label={t("machines.row_actions")}
            trigger={<IconButton size="sm" label={t("machines.row_actions")} icon={<MoreHorizontal size={15} />} />}
          >
            {!pending && !slot.busy ? (
              <MenuItem
                label={t(slot.status === "ready" ? "machines.relogin" : "machines.login_now")}
                onSelect={() => void act.run("login").then(() => onSignIn(slot))}
              />
            ) : null}
            {slot.status === "ready" ? (
              <MenuItem label={t("machines.verify")} onSelect={() => void act.run("verify")} />
            ) : null}
            {slot.status === "ready" && slot.volume.managed ? (
              <MenuItem label={t("machines.logout")} onSelect={() => void act.run("logout")} />
            ) : null}
            <MenuItem label={t("machines.rename")} onSelect={() => onRename(slot)} />
            {!slot.busy ? (
              <MenuItem danger label={t("machines.delete")} onSelect={() => onDelete(slot)} />
            ) : null}
          </Menu>
        </div>
      </div>
      {open ? (
        <div className="mc-panel" id={panel}>
          {!pending && (slot.status === "needs_login" || slot.status === "error") ? (
            <p className="note note--err" role="status">
              {reasonKey ? t(reasonKey) : t("machines.reason.generic", { reason: reason ?? slot.status })}
            </p>
          ) : null}
          {slot.status === "ready" && reasonKey ? <p className="note note--warn">{t(reasonKey)}</p> : null}
          {slot.status === "running" ? (
            <p className="note note--run" role="status">
              {t("machines.running_note")}{" "}
              {slot.worker ? <Link to={`/sessions/${slot.worker.session_id}`}>{t("machines.open_session")}</Link> : null}
            </p>
          ) : null}
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

/** The "Add machine" dialog: provider select + label, then the official login begins. */
function AddMachineDialog({
  providers,
  pending,
  onClose,
  onAdd,
}: {
  providers: SubscriptionProvider[];
  pending: boolean;
  onClose: () => void;
  onAdd: (provider: string, label: string) => void;
}) {
  const { t } = useI18n();
  const [provider, setProvider] = useState(providers[0]?.provider_id ?? "");
  const [label, setLabel] = useState("");
  return (
    <Dialog title={t("machines.add_title")} onClose={onClose} width={480}>
      <form
        aria-label={t("machines.add_title")}
        onSubmit={(e) => {
          e.preventDefault();
          if (provider) onAdd(provider, label.trim());
        }}
      >
        <label className="field">
          <span className="field__label">{t("machines.provider")}</span>
          <Select value={provider} onChange={(e) => setProvider(e.target.value)}>
            {providers.map((p) => (
              <option key={p.provider_id} value={p.provider_id}>
                {p.display_name} —{" "}
                {t(p.login_method === "authorization_code" ? "machines.add_hint_paste" : "machines.add_hint_code", {
                  host: p.verification_host,
                })}
              </option>
            ))}
          </Select>
        </label>
        <label className="field">
          <span className="field__label">{t("machines.name")}</span>
          <input className="input" value={label} maxLength={80} onChange={(e) => setLabel(e.target.value)} />
        </label>
        <div className="mc-actions">
          <Button type="submit" variant="primary" disabled={!provider || pending}>
            {t("machines.add_any")}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
        </div>
      </form>
    </Dialog>
  );
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
  const [loginId, setLoginId] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<MachineSlot | null>(null);
  const [deleting, setDeleting] = useState<MachineSlot | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [collapsed, setCollapsed] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? "[]") as string[];
    } catch {
      return [];
    }
  });
  const seen = useRef(false);
  const [rnLabel, setRnLabel] = useState("");
  const [rnAlias, setRnAlias] = useState("");
  const [delConfirm, setDelConfirm] = useState("");

  const toggleGroup = (provider: string) => {
    setCollapsed((current) => {
      const next = current.includes(provider) ? current.filter((p) => p !== provider) : [...current, provider];
      localStorage.setItem(COLLAPSED_KEY, JSON.stringify(next));
      return next;
    });
  };

  const add = useAction(async (key, provider: string, label: string) => {
    const slot = await api.slots.create(
      w!,
      { provider, ...(label ? { label } : {}) },
      { idempotencyKey: key },
    );
    setAddOpen(false);
    setOpenId(slot.id);
    setLoginId(slot.id);
    // The new machine must not be hidden by a filter, a search or a collapsed group.
    setFilter("all");
    setSearch("");
    setCollapsed((current) => current.filter((p) => p !== provider));
    q.refetch();
  });
  const rename = useAction(async (key) => {
    if (!renaming) return;
    await api.slots.update(
      renaming.id,
      { label: rnLabel.trim(), account_alias: rnAlias.trim() || null },
      { idempotencyKey: key },
    );
    setRenaming(null);
    q.refetch();
  });
  const remove = useAction(async (key) => {
    if (!deleting) return;
    await api.slots.remove(deleting.id, delConfirm, { idempotencyKey: key });
    setDeleting(null);
    q.refetch();
  });

  // A login already in progress when the page opens is shown without hunting for it.
  useEffect(() => {
    if (seen.current || !data) return;
    seen.current = true;
    const pending = data.items.find((s) => s.status === "login_pending");
    if (pending) setLoginId(pending.id);
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
  // A provider gets a group once it has a machine; a filter that matches none says so in place.
  const groups = providers
    .filter((p) => (data?.items ?? []).some((s) => s.provider === p.provider_id))
    .map((p) => ({ provider: p, slots: items.filter((s) => s.provider === p.provider_id) }));
  const total = data?.summary.total ?? 0;
  const loginSlot = data?.items.find((s) => s.id === loginId && s.status === "login_pending" && s.login) ?? null;

  return (
    <div className="page">
      <PageHeader
        title={t("nav.machines")}
        actions={
          <Button
            variant="primary"
            size="sm"
            icon={<Plus size={14} />}
            disabled={add.pending || !w || available.length === 0}
            onClick={() => setAddOpen(true)}
          >
            {available.length <= 1
              ? t("machines.add", { provider: available[0]?.display_name ?? "Codex" })
              : t("machines.add_any")}
          </Button>
        }
      />
      <p className="page__lead">{t("machines.lead")}</p>

      {q.loading && !data ? <Spinner /> : null}
      <ErrorNotice error={q.error ?? add.error ?? rename.error ?? remove.error} onRetry={q.error ? q.refetch : undefined} />
      {data && available.length === 0 ? (
        <p className="note note--warn" role="status">
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
              <Search size={13} />
              <input
                type="search"
                aria-label={t("machines.search")}
                placeholder={t("machines.search")}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
            <Select aria-label={t("machines.filter")} value={filter} onChange={(e) => setFilter(e.target.value as Filter)}>
              {(["all", "ready", "running", "login_pending", "attention"] as Filter[]).map((f) => (
                <option key={f} value={f}>
                  {t(`machines.filter.${f}` as I18nKey)}
                </option>
              ))}
            </Select>
            <Select aria-label={t("machines.sort")} value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
              {(["name", "status", "recent"] as Sort[]).map((s) => (
                <option key={s} value={s}>
                  {t(`machines.sort.${s}` as I18nKey)}
                </option>
              ))}
            </Select>
          </div>
        </>
      ) : null}

      {data && total === 0 && available.length > 0 ? (
        <div className="mc-empty">
          <Cpu size={22} />
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
              <section className="mc-group page__section" key={provider.provider_id} aria-label={provider.display_name}>
                <h2>
                  <button
                    type="button"
                    className="mc-group-head"
                    aria-expanded={!isCollapsed}
                    aria-controls={body}
                    onClick={() => toggleGroup(provider.provider_id)}
                  >
                    <ChevronDown size={12} style={{ transform: isCollapsed ? "rotate(-90deg)" : "none" }} />
                    <span>{provider.display_name}</span>
                    <small>{t("machines.group_count", { count: all.length, running })}</small>
                  </button>
                </h2>
                {!isCollapsed ? (
                  slots.length ? (
                    <ul className="lcard mc-list" id={body} style={{ padding: 0, listStyle: "none", margin: 0 }}>
                      {slots.map((slot) => (
                        <SlotRow
                          key={slot.id}
                          slot={slot}
                          open={openId === slot.id}
                          loginOpen={loginId === slot.id}
                          onToggle={() => setOpenId(openId === slot.id ? null : slot.id)}
                          onChanged={q.refetch}
                          onRename={(s) => {
                            setRnLabel(s.label);
                            setRnAlias(s.account_alias ?? "");
                            setRenaming(s);
                          }}
                          onDelete={(s) => {
                            setDelConfirm("");
                            setDeleting(s);
                          }}
                          onSignIn={(s) => setLoginId(s.id)}
                        />
                      ))}
                    </ul>
                  ) : (
                    <p className="note" id={body}>
                      {t("machines.no_match")}
                    </p>
                  )
                ) : null}
              </section>
            );
          })
        : null}

      {addOpen ? (
        <AddMachineDialog
          providers={available}
          pending={add.pending}
          onClose={() => setAddOpen(false)}
          onAdd={(provider, label) => void add.run(provider, label)}
        />
      ) : null}

      {loginSlot ? (
        <Dialog title={t("machines.login.title")} onClose={() => setLoginId(null)} width={480}>
          <LoginPanel
            slot={loginSlot}
            onChanged={() => {
              q.refetch();
            }}
          />
        </Dialog>
      ) : null}

      {renaming ? (
        <Dialog title={t("machines.rename")} onClose={() => setRenaming(null)} width={420}>
          <form
            className="mc-form"
            aria-label={t("machines.rename")}
            onSubmit={(e) => {
              e.preventDefault();
              if (rnLabel.trim()) void rename.run();
            }}
          >
            <label className="field">
              <span className="field__label">{t("machines.name")}</span>
              <input className="input" value={rnLabel} maxLength={80} onChange={(e) => setRnLabel(e.target.value)} autoFocus />
            </label>
            <label className="field">
              <span className="field__label">{t("machines.alias")}</span>
              <input
                className="input"
                value={rnAlias}
                maxLength={80}
                placeholder={t("machines.alias_ph")}
                onChange={(e) => setRnAlias(e.target.value)}
              />
            </label>
            <div className="mc-actions">
              <Button type="submit" variant="primary" disabled={rename.pending || !rnLabel.trim()}>
                {t("machines.save")}
              </Button>
              <Button variant="ghost" onClick={() => setRenaming(null)}>
                {t("common.cancel")}
              </Button>
            </div>
          </form>
        </Dialog>
      ) : null}

      {deleting ? (
        <Dialog title={t("machines.remove_title")} onClose={() => setDeleting(null)} width={420}>
          <form
            className="mc-form"
            aria-label={t("machines.remove_title")}
            onSubmit={(e) => {
              e.preventDefault();
              if (delConfirm === deleting.label) void remove.run();
            }}
          >
            <p className="note note--err">
              {t(deleting.volume.managed ? "machines.delete_warn" : "machines.delete_warn_adopted", { name: deleting.label })}
            </p>
            <label className="field">
              <span className="field__label">{t("machines.delete_confirm", { name: deleting.label })}</span>
              <input
                className="input"
                aria-label={t("machines.delete_confirm", { name: deleting.label })}
                value={delConfirm}
                onChange={(e) => setDelConfirm(e.target.value)}
                autoFocus
              />
            </label>
            <div className="mc-actions">
              <Button type="submit" variant="danger" disabled={delConfirm !== deleting.label || remove.pending}>
                {t("machines.delete")}
              </Button>
              <Button variant="ghost" onClick={() => setDeleting(null)}>
                {t("common.cancel")}
              </Button>
            </div>
            <ErrorNotice error={remove.error} />
          </form>
        </Dialog>
      ) : null}
    </div>
  );
}

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { GitBranch, Camera, Download, RotateCw } from "lucide-react";
import { Link } from "react-router-dom";
import type { ChangeSet, ChangeSetFile, Delivery, DeliveryCapability } from "../../../api/types";
import { isApiError } from "../../../api/errors";
import { useI18n } from "../../../i18n";
import type { I18nKey } from "../../../i18n/en";
import { useApi, useQueryClient } from "../../../state/context";
import { useQuery } from "../../../state/query";
import { Button, IconButton } from "../../../ui/Button";
import { ErrorNotice } from "../../../ui/ErrorNotice";
import { Field, Input } from "../../../ui/Field";
import { Pill, type Tone } from "../../../ui/Pill";
import { Skeleton } from "../../../ui/Skeleton";
import { useAction } from "../../../ui/useAction";
import { when } from "../../../ui/format";
import { parseUnifiedDiff, type DiffFile } from "../diff";

/** Git porcelain status code as a word; unknown codes stay "changed". */
export function statusKind(status: string): "new" | "modified" | "deleted" | "renamed" | "changed" {
  const code = status.trim();
  if (code === "??" || code.startsWith("A")) return "new";
  if (code.includes("D")) return "deleted";
  if (code.startsWith("R")) return "renamed";
  if (code.includes("M")) return "modified";
  return "changed";
}

// "changed" is honest about not knowing yet (no patch parsed, or an unusual porcelain code).
const LETTER: Record<string, string> = { new: "A", modified: "M", deleted: "D", renamed: "R", changed: "•" };

/** How often the Live view re-observes the worktree while it is on screen. */
export const LIVE_POLL_MS = 10_000;

const ROW_CLASS: Record<string, string> = {
  meta: "diff__line diff__line--meta",
  hunk: "diff__line diff__line--hunk",
  add: "diff__line diff__line--add",
  del: "diff__line diff__line--del",
  nonewline: "diff__line diff__line--meta",
  context: "diff__line",
};

/** Unified diff with real source line numbers: headers and markers carry none. */
export function DiffView({ diff, files, truncated }: { diff?: string; files?: DiffFile[]; truncated?: boolean }) {
  const { t } = useI18n();
  const parsed = useMemo(() => files ?? parseUnifiedDiff(diff ?? ""), [files, diff]);
  return (
    <div>
      <pre className="diff" aria-label={t("changes.diff")}>
        {parsed.flatMap((f, fi) =>
          f.rows.map((row, i) => (
            <span key={`${fi}:${i}`} className={ROW_CLASS[row.kind]} data-kind={row.kind}>
              <span className="diff__no">{row.old ?? ""}</span>
              <span className="diff__no">{row.new ?? ""}</span>
              <span className="diff__code">{row.text}</span>
              {"\n"}
            </span>
          )),
        )}
      </pre>
      {truncated ? <p className="faint small">{t("changes.diff_truncated")}</p> : null}
    </div>
  );
}

function word(t: ReturnType<typeof useI18n>["t"], prefix: string, value: string): string {
  const key = `${prefix}.${value}` as I18nKey;
  return t(key) === key ? value.replaceAll("_", " ") : t(key);
}

const stateTone = (s: string): Tone =>
  s === "succeeded" || s === "merged" ? "ok" : s === "failed" || s === "cancelled" ? "err" : s === "blocked" ? "warn" : "run";

const safeUrl = (u: string | null | undefined) => (u && /^https?:\/\//i.test(u) ? u : null);

/**
 * One Delivery with its server-computed merge eligibility. The UI never decides
 * eligibility: it renders `merge_eligibility` and sends exactly its pins.
 */
export function DeliveryCard({ delivery: d, onChanged }: { delivery: Delivery; onChanged: () => void }) {
  const { t } = useI18n();
  const api = useApi();
  const [markReady, setMarkReady] = useState(false);
  const elig = d.merge_eligibility;
  const merge = useAction(async (key) => {
    await api.deliveries.requestMerge(
      d.id,
      {
        expected_head_sha: elig.head_sha ?? "",
        subject_digest: elig.subject_digest ?? "",
        expected_version: d.version,
        method: "squash",
        ...(markReady ? { mark_ready: true } : {}),
      },
      { idempotencyKey: key },
    );
    onChanged();
  });
  const retry = useAction(async (key) => {
    await api.deliveries.retry(d.id, { idempotencyKey: key });
    onChanged();
  });
  const refresh = useAction(async (key) => {
    await api.deliveries.refresh(d.id, { idempotencyKey: key });
    onChanged();
  });
  const pr = d.pull_request;
  const prUrl = safeUrl(pr?.url);

  return (
    <div className="delivery" role="group" aria-label={t("delivery.aria", { id: d.id.slice(-6) })}>
      <div className="delivery__head">
        <strong>{word(t, "delivery.transport", d.transport)}</strong>
        <Pill tone={stateTone(d.state)}>{word(t, "delivery.state", d.state)}</Pill>
      </div>
      {d.state_reason ? <p className="small muted">{d.state_reason}</p> : null}
      <dl className="kv">
        {d.commit_sha ? (
          <>
            <dt>{t("delivery.commit")}</dt>
            <dd>
              <code>{d.commit_sha.slice(0, 12)}</code>
            </dd>
          </>
        ) : null}
        {pr ? (
          <>
            <dt>{t("delivery.pr")}</dt>
            <dd>
              {prUrl ? (
                <a href={prUrl} target="_blank" rel="noopener noreferrer">
                  {t("delivery.open_pr", { n: pr.number })}
                </a>
              ) : (
                `#${pr.number}`
              )}{" "}
              · {pr.state}
              {pr.draft ? ` · ${t("delivery.draft")}` : ""}
            </dd>
          </>
        ) : null}
        {d.remote?.checks_state ? (
          <>
            <dt>{t("delivery.checks")}</dt>
            <dd>{d.remote.checks_state}</dd>
          </>
        ) : null}
      </dl>
      {d.steps.length ? (
        <ol className="delivery__steps" aria-label={t("delivery.steps")}>
          {d.steps.map((s, i) => (
            <li key={i}>
              {word(t, "delivery.step", s.kind)} <span className="faint">· {word(t, "delivery.outcome", s.outcome)}</span>
            </li>
          ))}
        </ol>
      ) : null}
      {d.merge_requests.map((m) => (
        <div key={m.id} className="small">
          {t("delivery.merge_request")}: <Pill tone={stateTone(m.state)}>{m.state}</Pill>
          {m.merge_sha ? <code> {m.merge_sha.slice(0, 12)}</code> : null}
        </div>
      ))}

      <div className="delivery__gate" aria-label={t("delivery.eligibility")}>
        {elig.eligible ? (
          <p className="small" role="status">
            <Pill tone="ok">{t("delivery.eligible")}</Pill>
          </p>
        ) : (
          <div className="small">
            <Pill tone="warn">{t("delivery.not_eligible")}</Pill>
            <ul className="delivery__reasons">
              {elig.reasons.map((r) => (
                <li key={r} title={r}>
                  {word(t, "delivery.reason", r.split(":")[0])}
                  {r.includes(":") ? <code> {r.split(":").slice(1).join(":")}</code> : null}
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="faint small">
          {t("delivery.observed")}: {when(elig.observed_at)}
        </div>
      </div>

      <ErrorNotice error={merge.error ?? retry.error ?? refresh.error} />
      <div className="delivery__actions">
        {pr?.draft ? (
          <label className="small check">
            <input type="checkbox" checked={markReady} onChange={(e) => setMarkReady(e.target.checked)} />
            {t("delivery.mark_ready")}
          </label>
        ) : null}
        <Button size="sm" variant="primary" disabled={!elig.eligible || merge.pending} onClick={() => void merge.run()}>
          {t("delivery.merge")}
        </Button>
        <Button size="sm" variant="secondary" disabled={refresh.pending} onClick={() => void refresh.run()}>
          {t("delivery.refresh")}
        </Button>
        {d.state === "failed" || d.state === "blocked" ? (
          <Button size="sm" variant="secondary" disabled={retry.pending} onClick={() => void retry.run()}>
            {t("delivery.retry")}
          </Button>
        ) : null}
      </div>
    </div>
  );
}

type Kind = ReturnType<typeof statusKind>;

/**
 * Live observations carry a porcelain code. A ChangeSet manifest only knows "deleted"
 * or the entry type ("file", "symlink"): new vs modified comes from the parsed patch.
 */
export function fileKind(f: ChangeSetFile | { path: string; status: string }, patch?: DiffFile | null): Kind {
  if ("status" in f) return statusKind(f.status);
  if (f.type === "deleted") return "deleted";
  if (patch) return patch.status;
  return ["new", "modified", "renamed"].includes(f.type) ? (f.type as Kind) : "changed";
}

function FileRow({ f, kind, active, counts, onSelect }: { f: ChangeSetFile | { path: string; status: string }; kind: Kind; active: boolean; counts?: { add: number; del: number } | null; onSelect: () => void }) {
  const { t } = useI18n();
  const name = f.path.split("/").pop() ?? f.path;
  const dir = f.path.slice(0, Math.max(0, f.path.length - name.length - 1));
  return (
    <li>
      <button type="button" className={`chg-file${active ? " chg-file--active" : ""}`} aria-pressed={active} onClick={onSelect}>
        <span className={`chg-badge chg-badge--${kind}`} title={t(`changes.status.${kind}` as I18nKey)}>
          {LETTER[kind]}
        </span>
        <span className="chg-file__path truncate">
          {dir ? <span className="faint">{dir}/</span> : null}
          {name}
        </span>
        {counts ? (
          <span className="chg-file__counts">
            <span className="chg-add">+{counts.add}</span> <span className="chg-del">−{counts.del}</span>
          </span>
        ) : null}
      </button>
    </li>
  );
}

/** Live observation (never wakes compute) + captured ChangeSets + Deliveries. */
export function ChangesTab({ sessionId, revision = 0 }: { sessionId: string; revision?: number }) {
  const { t } = useI18n();
  const api = useApi();
  const qc = useQueryClient();
  const [selectedSet, setSelectedSet] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const sets = useQuery(["changesets", sessionId, revision], () => api.changes.list(sessionId));
  const items = sets.data?.items ?? [];
  const current = items.find((c) => c.id === selectedSet) ?? items[0] ?? null;
  const liveMode = !current || selectedSet === "live";
  // Terminal commands change the worktree without any event: while the Live view is on
  // screen it re-observes on activation and at a low rate, and Refresh is explicit.
  const liveQ = useQuery(["changes-live", sessionId, revision], () => api.changes.live(sessionId), {
    pollMs: liveMode ? LIVE_POLL_MS : undefined,
  });
  const refetchLive = liveQ.refetch;
  useEffect(() => {
    if (liveMode) refetchLive();
  }, [liveMode, refetchLive]);
  const capture = useAction(async (key, origin: "explicit" | "salvage") => {
    const r = await api.changes.capture(sessionId, { origin }, { idempotencyKey: key });
    qc.invalidate(["changesets", sessionId]);
    setSelectedSet(r.changeset.id);
  });
  const unavailable = isApiError(liveQ.error) && liveQ.error.code === "executor_unavailable";
  const liveFiles = liveQ.data?.observation.files ?? [];

  return (
    <div className="chg">
      <div className="chg__toolbar">
        <GitBranch size={14} className="faint" />
        <select
          className="select chg__scope"
          aria-label={t("changes.pick_changeset")}
          title={liveMode ? t("changes.live") : `${current.id.slice(-6)} · ${current.origin} · ${current.state}`}
          value={liveMode ? "live" : current.id}
          onChange={(e) => setSelectedSet(e.target.value === "live" ? "live" : e.target.value)}
        >
          <option value="live">{t("changes.live")}</option>
          {items.map((c) => (
            <option key={c.id} value={c.id}>
              {c.id.slice(-6)} · {c.origin} · {c.state}
            </option>
          ))}
        </select>
        <span className="chg__spacer" />
        {liveMode ? (
          <IconButton
            size="sm"
            icon={<RotateCw size={13} />}
            label={t("common.refresh")}
            title={t("changes.refresh_hint")}
            disabled={liveQ.loading}
            onClick={() => {
              refetchLive();
              sets.refetch();
            }}
          />
        ) : null}
        <Button size="sm" variant="ghost" disabled={capture.pending} onClick={() => void capture.run("salvage")}>
          {t("changes.salvage")}
        </Button>
        <Button size="sm" variant="secondary" icon={<Camera size={13} />} disabled={capture.pending} onClick={() => void capture.run("explicit")}>
          {t("changes.capture")}
        </Button>
      </div>
      <ErrorNotice error={capture.error} />

      {liveMode ? (
        <section className="chg__live" aria-label={t("changes.live")}>
          {liveQ.loading && !liveQ.data ? <Skeleton height={66} /> : null}
          {unavailable ? (
            <div className="notice notice--warn" role="status">
              <div className="notice__body">
                <div className="notice__title">{t("changes.unavailable")}</div>
                <div className="notice__text">{t("changes.unavailable_body")}</div>
              </div>
            </div>
          ) : (
            <ErrorNotice error={liveQ.error} onRetry={liveQ.refetch} />
          )}
          {liveQ.data ? (
            liveFiles.length ? (
              <ul className="chg__files" aria-label={t("changes.live")}>
                {liveFiles.map((f) => (
                  <FileRow key={f.path} f={f} kind={fileKind(f)} active={false} onSelect={() => undefined} />
                ))}
              </ul>
            ) : (
              <p className="muted small">{t("changes.clean")}</p>
            )
          ) : null}
        </section>
      ) : (
        <ChangeSetDetail key={current.id} cs={current} rev={revision} selectedFile={selectedFile} onSelectFile={setSelectedFile} />
      )}
    </div>
  );
}

/** Why this ChangeSet cannot be delivered, and only the alternatives that really exist. */
function DeliveryUnavailable({ cs, capability }: { cs: ChangeSet; capability: DeliveryCapability }) {
  const { t } = useI18n();
  const api = useApi();
  const reason = capability.reason ?? "not_ready";
  if (reason === "not_ready") return <p className="muted small">{t("delivery.blocked.not_ready")}</p>;
  return (
    <div className="notice notice--warn" role="status" data-reason={reason}>
      <div className="notice__body">
        <div className="notice__title">{t(`delivery.blocked.${reason}.title` as I18nKey)}</div>
        <div className="notice__text">
          {t(`delivery.blocked.${reason}.body` as I18nKey, { repo: capability.repository ?? "", transport: capability.transport })}
        </div>
        <div className="chg-detail__recovery">
          {capability.recovery.includes("download_patch") ? (
            <a className="btn btn--secondary btn--sm" href={api.changes.patchUrl(cs.id)} download={`${cs.id}.patch`}>
              <Download size={13} /> {t("delivery.download_patch")}
            </a>
          ) : null}
          {capability.recovery.includes("manage_connections") ? (
            <Link className="btn btn--secondary btn--sm" to="/connections">
              {t("delivery.manage_connections")}
            </Link>
          ) : null}
        </div>
        {capability.recovery.includes("download_patch") ? <p className="faint small">{t("delivery.patch_hint")}</p> : null}
      </div>
    </div>
  );
}

function ChangeSetDetail({
  cs,
  rev,
  selectedFile,
  onSelectFile,
}: {
  cs: ChangeSet;
  rev: number;
  selectedFile: string | null;
  onSelectFile: (path: string | null) => void;
}) {
  const { t } = useI18n();
  const api = useApi();
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [draftChoice, setDraft] = useState<boolean | null>(null);
  // Diffs stay lazy: fetched on demand (Show diff) or once a file is picked.
  const [showDiff, setShowDiff] = useState(false);
  const wantDiff = showDiff || selectedFile !== null;
  const detail = useQuery(["changeset", cs.id, rev], () => api.changes.get(cs.id));
  const diff = useQuery(wantDiff ? ["changeset-diff", cs.id, cs.subject_digest ?? ""] : null, () => api.changes.diff(cs.id));
  const deliveries = useQuery(["deliveries", cs.id, rev], () => api.deliveries.list(cs.id));
  const reload = () => qc.invalidate(["deliveries", cs.id]);
  // The server's own verdict; an older server without one keeps the previous behaviour.
  const capability = detail.data?.delivery ?? cs.delivery;
  const draft = draftChoice ?? capability?.draft ?? true;
  const request = useAction(async (key) => {
    await api.deliveries.request(cs.id, { ...(title.trim() ? { title: title.trim() } : {}), draft }, { idempotencyKey: key });
    reload();
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void request.run();
  };
  const files = detail.data?.files ?? cs.files ?? [];
  const patches = useMemo(() => (diff.data ? parseUnifiedDiff(diff.data.diff) : []), [diff.data]);
  const byPath = useMemo(() => new Map(patches.map((p) => [p.path, p])), [patches]);
  const shownFile = files.find((f) => f.path === selectedFile) ? selectedFile : (files[0]?.path ?? null);
  const shownPatch = (shownFile ? byPath.get(shownFile) : undefined) ?? (patches.length === 1 ? patches[0] : null);
  const totals = useMemo(
    () => patches.reduce((sum, p) => ({ add: sum.add + p.add, del: sum.del + p.del }), { add: 0, del: 0 }),
    [patches],
  );

  return (
    <div className="chg-detail">
      {cs.state !== "ready" && cs.error ? <Pill tone="err">{t("changes.capture_failed")}</Pill> : null}
      <p className="faint small">
        {cs.origin} · {cs.state} · {cs.file_count ?? files.length} {t("changes.files")} · {when(cs.created_at)}
        {diff.data ? ` · ${t("changes.summary", { files: files.length, add: totals.add, del: totals.del })}` : ""}
      </p>
      <ErrorNotice error={detail.error} onRetry={detail.refetch} />
      <div className="chg-detail__grid">
        <div className="chg-detail__list">
          <select
            className="select chg__filepick"
            aria-label={t("changes.select_file")}
            value={shownFile ?? ""}
            onChange={(e) => onSelectFile(e.target.value)}
          >
            {files.map((f) => (
              <option key={f.path} value={f.path}>
                {f.path}
              </option>
            ))}
          </select>
          <ul className="chg__files" aria-label={t("changes.files_title", { n: files.length })}>
            {files.map((f) => (
              <FileRow
                key={f.path}
                f={f}
                kind={fileKind(f, byPath.get(f.path))}
                active={f.path === shownFile}
                counts={byPath.get(f.path) ?? null}
                onSelect={() => onSelectFile(f.path)}
              />
            ))}
          </ul>
        </div>
        <div className="chg-detail__diff">
          {wantDiff ? null : (
            <Button size="sm" variant="secondary" onClick={() => setShowDiff(true)}>
              {t("changes.show_diff")}
            </Button>
          )}
          {wantDiff && diff.loading && !diff.data ? <Skeleton height={66} /> : null}
          {wantDiff ? <ErrorNotice error={diff.error} onRetry={diff.refetch} /> : null}
          {diff.data ? <DiffView files={shownPatch ? [shownPatch] : patches} truncated={diff.data.truncated} /> : null}
        </div>
      </div>

      <section className="chg-detail__deliveries" aria-label={t("delivery.heading")}>
        <h3 className="ov-card__title">{t("delivery.heading")}</h3>
        {(deliveries.data?.items ?? []).map((d) => (
          <DeliveryCard key={d.id} delivery={d} onChanged={reload} />
        ))}
        {deliveries.data && !deliveries.data.items.length ? <p className="muted small">{t("delivery.none")}</p> : null}
        {!capability || capability.available ? (
          <form className="chg-detail__request" onSubmit={submit} aria-label={t("delivery.request")}>
            {capability?.repository ? (
              <p className="faint small chg-detail__target">
                {t(
                  capability.transport === "git_branch"
                    ? "delivery.target_branch"
                    : draft
                      ? "delivery.target_draft_pr"
                      : "delivery.target_pr",
                  { repo: capability.repository, base: capability.base_branch ?? "main" },
                )}
              </p>
            ) : null}
            <Field
              label={t(capability?.transport === "git_branch" ? "delivery.commit_title" : "delivery.pr_title")}
              htmlFor={`dtitle-${cs.id}`}
            >
              <Input id={`dtitle-${cs.id}`} value={title} onChange={(e) => setTitle(e.target.value)} />
            </Field>
            {capability?.transport !== "git_branch" ? (
              <label className="small check">
                <input type="checkbox" checked={draft} onChange={(e) => setDraft(e.target.checked)} />
                {t("delivery.draft")}
              </label>
            ) : null}
            <Button size="sm" variant="primary" type="submit" disabled={request.pending || cs.state !== "ready"}>
              {t("delivery.request")}
            </Button>
          </form>
        ) : (
          <DeliveryUnavailable cs={cs} capability={capability} />
        )}
        <ErrorNotice error={request.error} />
      </section>
    </div>
  );
}

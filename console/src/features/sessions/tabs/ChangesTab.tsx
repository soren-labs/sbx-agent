import { useMemo, useState, type FormEvent } from "react";
import { GitBranch, Camera } from "lucide-react";
import type { ChangeSet, ChangeSetFile, Delivery } from "../../../api/types";
import { isApiError } from "../../../api/errors";
import { useI18n } from "../../../i18n";
import type { I18nKey } from "../../../i18n/en";
import { useApi, useQueryClient } from "../../../state/context";
import { useQuery } from "../../../state/query";
import { Button } from "../../../ui/Button";
import { ErrorNotice } from "../../../ui/ErrorNotice";
import { Field, Input } from "../../../ui/Field";
import { Pill, type Tone } from "../../../ui/Pill";
import { Skeleton } from "../../../ui/Skeleton";
import { useAction } from "../../../ui/useAction";
import { when } from "../../../ui/format";

/** Git porcelain status code as a word; unknown codes stay "changed". */
export function statusKind(status: string): "new" | "modified" | "deleted" | "renamed" | "changed" {
  const code = status.trim();
  if (code === "??" || code.startsWith("A")) return "new";
  if (code.includes("D")) return "deleted";
  if (code.startsWith("R")) return "renamed";
  if (code.includes("M")) return "modified";
  return "changed";
}

const LETTER: Record<string, string> = { new: "A", modified: "M", deleted: "D", renamed: "R", changed: "M" };

/** Sections of a unified diff, split at file headers; order preserved. */
function splitDiff(diff: string): { file: string | null; text: string }[] {
  const lines = diff.split("\n");
  const sections: { file: string | null; text: string }[] = [];
  let cur: { file: string | null; lines: string[] } = { file: null, lines: [] };
  const push = () => {
    if (cur.lines.length) sections.push({ file: cur.file, text: cur.lines.join("\n") });
  };
  for (const line of lines) {
    if (line.startsWith("diff --git ")) {
      push();
      const m = line.match(/^diff --git a\/(.+) b\/(.+)$/);
      cur = { file: m ? m[2] : null, lines: [line] };
    } else {
      cur.lines.push(line);
    }
  }
  push();
  return sections.filter((s) => s.text.trim());
}

/** +/− line counts of one diff section (file headers and +++/--- excluded). */
function diffCounts(text: string): { add: number; del: number } {
  let add = 0;
  let del = 0;
  for (const line of text.split("\n")) {
    if (line.startsWith("+") && !line.startsWith("+++")) add++;
    else if (line.startsWith("-") && !line.startsWith("---")) del++;
  }
  return { add, del };
}

export function DiffView({ diff, truncated }: { diff: string; truncated?: boolean }) {
  const { t } = useI18n();
  let oldNo = 0;
  let newNo = 0;
  return (
    <div>
      <pre className="diff" aria-label={t("changes.diff")}>
        {diff.split("\n").map((line, i) => {
          const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
          if (hunk) {
            oldNo = Number(hunk[1]);
            newNo = Number(hunk[2]);
          }
          let cls = "";
          let a = "";
          let b = "";
          if (line.startsWith("@@")) {
            cls = "diff__line diff__line--hunk";
          } else if (line.startsWith("+") && !line.startsWith("+++")) {
            cls = "diff__line diff__line--add";
            b = String(newNo++);
          } else if (line.startsWith("-") && !line.startsWith("---")) {
            cls = "diff__line diff__line--del";
            a = String(oldNo++);
          } else {
            a = String(oldNo++);
            b = String(newNo++);
          }
          return (
            <span key={i} className={cls || "diff__line"}>
              <span className="diff__no">{a}</span>
              <span className="diff__no">{b}</span>
              <span className="diff__code">{line}</span>
              {"\n"}
            </span>
          );
        })}
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

/** ChangeSet files carry a word ("modified"), live observations a porcelain code. */
function fileKind(value: string): ReturnType<typeof statusKind> {
  return ["new", "modified", "deleted", "renamed", "changed"].includes(value) ? (value as ReturnType<typeof statusKind>) : statusKind(value);
}

function FileRow({ f, active, counts, onSelect }: { f: ChangeSetFile | { path: string; status: string }; active: boolean; counts?: { add: number; del: number } | null; onSelect: () => void }) {
  const { t } = useI18n();
  const kind = fileKind("status" in f ? f.status : f.type);
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
  const liveQ = useQuery(["changes-live", sessionId, revision], () => api.changes.live(sessionId));
  const sets = useQuery(["changesets", sessionId, revision], () => api.changes.list(sessionId));
  const capture = useAction(async (key, origin: "explicit" | "salvage") => {
    const r = await api.changes.capture(sessionId, { origin }, { idempotencyKey: key });
    qc.invalidate(["changesets", sessionId]);
    setSelectedSet(r.changeset.id);
  });
  const unavailable = isApiError(liveQ.error) && liveQ.error.code === "executor_unavailable";
  const items = sets.data?.items ?? [];
  const current = items.find((c) => c.id === selectedSet) ?? items[0] ?? null;
  const liveFiles = liveQ.data?.observation.files ?? [];
  const liveMode = !current || selectedSet === "live";

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
                  <FileRow key={f.path} f={f} active={false} onSelect={() => undefined} />
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
  const [draft, setDraft] = useState(true);
  // Diffs stay lazy: fetched on demand (Show diff) or once a file is picked.
  const [showDiff, setShowDiff] = useState(false);
  const wantDiff = showDiff || selectedFile !== null;
  const detail = useQuery(["changeset", cs.id, rev], () => api.changes.get(cs.id));
  const diff = useQuery(wantDiff ? ["changeset-diff", cs.id, cs.subject_digest ?? ""] : null, () => api.changes.diff(cs.id));
  const deliveries = useQuery(["deliveries", cs.id, rev], () => api.deliveries.list(cs.id));
  const reload = () => qc.invalidate(["deliveries", cs.id]);
  const request = useAction(async (key) => {
    await api.deliveries.request(cs.id, { ...(title.trim() ? { title: title.trim() } : {}), draft }, { idempotencyKey: key });
    reload();
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void request.run();
  };
  const files = detail.data?.files ?? cs.files ?? [];
  const sections = useMemo(() => (diff.data ? splitDiff(diff.data.diff) : []), [diff.data]);
  const counts = useMemo(() => {
    const map = new Map<string, { add: number; del: number }>();
    for (const s of sections) if (s.file) map.set(s.file, diffCounts(s.text));
    return map;
  }, [sections]);
  const shownFile = files.find((f) => f.path === selectedFile) ? selectedFile : (files[0]?.path ?? null);
  const shownSection = sections.find((s) => s.file === shownFile) ?? (sections.length === 1 ? sections[0] : null);
  const totals = useMemo(() => {
    let add = 0;
    let del = 0;
    for (const c of counts.values()) {
      add += c.add;
      del += c.del;
    }
    return { add, del };
  }, [counts]);

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
              <FileRow key={f.path} f={f} active={f.path === shownFile} counts={counts.get(f.path) ?? null} onSelect={() => onSelectFile(f.path)} />
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
          {diff.data ? (
            shownSection ? (
              <DiffView diff={shownSection.text} truncated={diff.data.truncated} />
            ) : (
              <DiffView diff={diff.data.diff} truncated={diff.data.truncated} />
            )
          ) : null}
        </div>
      </div>

      <section className="chg-detail__deliveries" aria-label={t("delivery.heading")}>
        <h3 className="ov-card__title">{t("delivery.heading")}</h3>
        {(deliveries.data?.items ?? []).map((d) => (
          <DeliveryCard key={d.id} delivery={d} onChanged={reload} />
        ))}
        {deliveries.data && !deliveries.data.items.length ? <p className="muted small">{t("delivery.none")}</p> : null}
        <form className="chg-detail__request" onSubmit={submit} aria-label={t("delivery.request")}>
          <Field label={t("delivery.pr_title")} htmlFor={`dtitle-${cs.id}`}>
            <Input id={`dtitle-${cs.id}`} value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <label className="small check">
            <input type="checkbox" checked={draft} onChange={(e) => setDraft(e.target.checked)} />
            {t("delivery.draft")}
          </label>
          <Button size="sm" variant="primary" type="submit" disabled={request.pending || cs.state !== "ready"}>
            {t("delivery.request")}
          </Button>
        </form>
        <ErrorNotice error={request.error} />
      </section>
    </div>
  );
}

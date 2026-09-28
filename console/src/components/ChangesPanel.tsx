import { useCallback, useEffect, useState } from "react";
import { isApiError } from "../api";
import type {
  DeliverInput,
  Session,
  SessionChange,
  SessionChangesDiff,
  SessionDeliverResult,
  SessionDiffFile,
} from "../api/types";
import { useI18n } from "../i18n";
import type { I18nKey } from "../i18n/en";
import { useApi } from "../state/api";
import { Icon, Spinner } from "./icons";

export function shortSha(sha?: string): string {
  return sha ? sha.slice(0, 7) : "—";
}

function diffLineClass(line: string): string {
  if (
    line.startsWith("diff --git") ||
    line.startsWith("index ") ||
    line.startsWith("new file") ||
    line.startsWith("deleted file") ||
    line.startsWith("similarity") ||
    line.startsWith("rename") ||
    line.startsWith("Binary files") ||
    line.startsWith("---") ||
    line.startsWith("+++")
  ) {
    return "d-meta";
  }
  if (line.startsWith("@@")) return "d-hunk";
  if (line.startsWith("+")) return "d-add";
  if (line.startsWith("-")) return "d-del";
  return "d-ctx";
}

/** One file row — its unified diff is fetched lazily on first expand. */
function FileDiffRow({
  sessionId,
  file,
}: {
  sessionId: string;
  file: SessionDiffFile;
}) {
  const { t } = useI18n();
  const api = useApi();
  const [open, setOpen] = useState(false);
  const [body, setBody] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  const onToggle = (e: React.SyntheticEvent<HTMLDetailsElement>) => {
    const isOpen = e.currentTarget.open;
    setOpen(isOpen);
    if (isOpen && body == null && !loading) {
      setLoading(true);
      setFailed(false);
      api
        .getFileDiff(sessionId, file.path)
        .then((d) => setBody(d.diff))
        .catch(() => setFailed(true))
        .finally(() => setLoading(false));
    }
  };

  return (
    <details
      className="file-row"
      onToggle={onToggle}
      data-testid={`file-${file.path}`}
    >
      <summary>
        <span className={`fstatus s-${file.status}`}>
          {t(`changes.fstatus.${file.status}` as I18nKey)}
        </span>
        <span className="mono grow file-path">
          {file.oldPath ? `${file.oldPath} → ${file.path}` : file.path}
        </span>
        <span className="stat-add">+{file.additions}</span>
        <span className="stat-del">−{file.deletions}</span>
      </summary>
      {open &&
        (loading ? (
          <div className="diff-loading">
            <Spinner size={14} />
          </div>
        ) : failed ? (
          <div className="diff-loading faint">{t("changes.diff_failed")}</div>
        ) : body != null ? (
          <pre className="diff mono">
            {body.split("\n").map((line, i) => (
              <div key={i} className={diffLineClass(line)}>
                {line || " "}
              </div>
            ))}
          </pre>
        ) : null)}
    </details>
  );
}

export interface ChangesPanelProps {
  session: Session;
  changes: SessionChange[];
  /** POST /deliver through the page — returns `{session, revision}`. */
  onDeliver: (input?: DeliverInput) => Promise<SessionDeliverResult>;
}

/**
 * The Changes tab: file list + lazy per-file diffs + the delivery state
 * machine (ready → branch → push → pull request → ready/updated, or a
 * failed state whose retry re-delivers without rerunning the provider).
 */
export function ChangesPanel({ session, changes, onDeliver }: ChangesPanelProps) {
  const { t } = useI18n();
  const api = useApi();
  const [diff, setDiff] = useState<SessionChangesDiff | null>(null);
  const [diffLoading, setDiffLoading] = useState(true);
  const [diffMissing, setDiffMissing] = useState(false);
  const [working, setWorking] = useState(false);
  const [failedMsg, setFailedMsg] = useState<string | null>(null);
  const [prUpdated, setPrUpdated] = useState(false);
  const [github, setGithub] = useState<boolean | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [draft, setDraft] = useState(false);

  // File-level view of the latest durable revision. Refetches when the
  // session updates (a follow-up turn materializes a newer revision, so
  // "latest changes" always means latest).
  useEffect(() => {
    let live = true;
    setDiffLoading(true);
    api
      .listChangesDiff(session.id)
      .then((d) => {
        if (live) {
          setDiff(d);
          setDiffMissing(false);
        }
      })
      .catch(() => {
        // revision_not_found (still materializing / nothing durable yet)
        // is a quiet empty state, not a page error.
        if (live) setDiffMissing(true);
      })
      .finally(() => {
        if (live) setDiffLoading(false);
      });
    return () => {
      live = false;
    };
  }, [api, session.id, session.updatedAt]);

  useEffect(() => {
    let live = true;
    api
      .getIntegrations()
      .then((i) => {
        if (live) setGithub(i.github.connected);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [api]);

  const connectGithub = useCallback(async () => {
    setConnecting(true);
    try {
      const { url } = await api.beginGithubAuthorize();
      window.open(url, "_blank", "noopener,noreferrer");
      // Recheck once the user returns from the install flow tab.
      const recheck = () => {
        window.removeEventListener("focus", recheck);
        api
          .getIntegrations()
          .then((i) => setGithub(i.github.connected))
          .catch(() => {});
      };
      window.addEventListener("focus", recheck);
    } finally {
      setConnecting(false);
    }
  }, [api]);

  const deliver = useCallback(async () => {
    const hadPr = session.delivery?.prNumber != null;
    setWorking(true);
    setFailedMsg(null);
    try {
      const res = await onDeliver({
        title: session.title,
        draft,
        target: session.repo?.ref,
      });
      setPrUpdated(hadPr && res.revision.prNumber != null);
    } catch (e) {
      if (isApiError(e) && e.kind === "github_required") {
        setGithub(false);
      } else {
        setFailedMsg(isApiError(e) ? e.message : String(e));
      }
    } finally {
      setWorking(false);
    }
  }, [onDeliver, draft, session.delivery?.prNumber, session.title]);

  const d = session.delivery;
  const pr =
    d?.prNumber != null && d.prUrl
      ? { number: d.prNumber, url: d.prUrl, state: d.prState }
      : null;
  const pushedSha = d?.pushedHeadSha ?? d?.prHeadSha;
  const stalePR = Boolean(
    pr && diff?.headSha && pushedSha && pushedSha !== diff.headSha,
  );
  const lastRev = [...changes].reverse().find((c) => c.kind === "revision");
  const persistedFail =
    d?.status === "failed" || lastRev?.deliveryStatus === "failed";
  const failedReason =
    failedMsg ??
    (typeof d?.error === "object" ? d.error?.message : d?.error) ??
    lastRev?.error ??
    null;
  const deliveredBranch =
    !pr && d?.status === "delivered" && d.branch
      ? d.branch
      : !pr && lastRev?.deliveryStatus === "delivered"
        ? (lastRev.branch ?? null)
        : null;

  return (
    <div data-testid="changes-panel">
      {diffLoading ? null : diff ? (
        <div className="changes-head" data-testid="changes-summary">
          <strong>
            {t("changes.summary", { files: diff.filesChanged })}
          </strong>
          <span className="stat-add">+{diff.additions}</span>
          <span className="stat-del">−{diff.deletions}</span>
          {session.phase === "running" && (
            <span className="faint small">{t("changes.in_progress")}</span>
          )}
        </div>
      ) : null}

      {working ? (
        <div className="card deliver-card" data-testid="deliver-card">
          <div className="d-title">
            <Spinner size={14} /> {t("deliver.working")}
          </div>
          <ol className="deliver-steps">
            <li>{t("deliver.step_branch")}</li>
            <li>{t("deliver.step_push")}</li>
            <li>{t("deliver.step_pr")}</li>
          </ol>
        </div>
      ) : (
        <>
          {pr && (
            <div
              className="card deliver-card delivered"
              data-testid="deliver-card"
            >
              <div className="d-title">
                <Icon name="github" size={15} />
                {prUpdated ? t("deliver.pr_updated") : t("deliver.pr_ready")}
                {pr.state && <span className="pill pill-idle">{pr.state}</span>}
              </div>
              <div className="d-body">
                <a
                  href={pr.url}
                  target="_blank"
                  rel="noreferrer"
                  className="mono"
                >
                  #{pr.number}
                </a>{" "}
                · {session.title}
              </div>
              {d?.branch && (
                <div className="faint small">
                  {t("deliver.pushed", {
                    branch: d.branch,
                    base: d.prBase ?? session.repo?.ref ?? "main",
                  })}
                </div>
              )}
              <div className="d-actions">
                <a
                  className="btn btn-sm"
                  href={pr.url}
                  target="_blank"
                  rel="noreferrer"
                  data-testid="open-github"
                >
                  {t("deliver.open_github")}
                </a>
                {stalePR && (
                  <button
                    className="btn btn-sm btn-primary"
                    onClick={() => void deliver()}
                    data-testid="deliver-update"
                  >
                    {t("deliver.update_pr")}
                  </button>
                )}
              </div>
              {stalePR && (
                <div className="faint small">{t("deliver.new_changes")}</div>
              )}
            </div>
          )}

          {(failedReason != null || persistedFail) && (
            <div
              className="card deliver-card failed"
              data-testid="deliver-failed"
            >
              <div className="d-title">{t("deliver.failed")}</div>
              <div className="d-body">{failedReason ?? t("deliver.failed")}</div>
              <div className="d-actions">
                <button
                  className="btn btn-sm"
                  onClick={() => void deliver()}
                  data-testid="deliver-retry"
                >
                  {t("deliver.retry")}
                </button>
              </div>
            </div>
          )}

          {!pr && !failedReason && !persistedFail &&
            (deliveredBranch ? (
              <div
                className="card deliver-card delivered"
                data-testid="deliver-card"
              >
                <div className="d-title">
                  {t("deliver.pushed_branch", { branch: deliveredBranch })}
                </div>
                {github !== false && (
                  <div className="d-actions">
                    <button
                      className="btn btn-sm btn-primary"
                      onClick={() => void deliver()}
                      data-testid="deliver-create"
                    >
                      {t("deliver.create_pr")}
                    </button>
                    <label className="draft-check">
                      <input
                        type="checkbox"
                        checked={draft}
                        onChange={(e) => setDraft(e.target.checked)}
                      />
                      {t("deliver.draft")}
                    </label>
                  </div>
                )}
              </div>
            ) : github === false ? (
              <div className="card deliver-card" data-testid="deliver-card">
                <div className="d-title">
                  <Icon name="github" size={15} />
                  {t("deliver.github_needed")}
                </div>
                <div className="d-actions">
                  <button
                    className="btn btn-sm btn-primary"
                    disabled={connecting}
                    onClick={() => void connectGithub()}
                    data-testid="connect-github"
                  >
                    {connecting ? <Spinner size={14} /> : null}
                    {t("integrations.connect_github")}
                  </button>
                </div>
              </div>
            ) : (
              <div className="card deliver-card" data-testid="deliver-card">
                <div className="d-title">{t("deliver.ready")}</div>
                <div className="d-actions">
                  <button
                    className="btn btn-sm btn-primary"
                    onClick={() => void deliver()}
                    data-testid="deliver-create"
                  >
                    {t("deliver.create_pr")}
                  </button>
                  <label className="draft-check">
                    <input
                      type="checkbox"
                      checked={draft}
                      onChange={(e) => setDraft(e.target.checked)}
                    />
                    {t("deliver.draft")}
                  </label>
                </div>
              </div>
            ))}
        </>
      )}

      {diffLoading ? (
        <div className="card diff-loading">
          <Spinner size={14} /> {t("common.loading")}
        </div>
      ) : diff ? (
        <div className="card file-card" data-testid="file-list">
          {diff.files.map((f) => (
            <FileDiffRow key={f.path} sessionId={session.id} file={f} />
          ))}
          {diff.files.length === 0 && (
            <div className="faint">{t("changes.no_diff")}</div>
          )}
        </div>
      ) : diffMissing ? (
        <div className="card faint">{t("changes.no_diff")}</div>
      ) : null}
    </div>
  );
}

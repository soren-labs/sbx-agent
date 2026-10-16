import { useState, type FormEvent } from "react";
import { ArrowLeft, Check, File as FileIcon, FilePen, Folder, RotateCw, X } from "lucide-react";
import type { FileContent, Session } from "../../../api/types";
import { isApiError } from "../../../api/errors";
import { useI18n } from "../../../i18n";
import { useApi } from "../../../state/context";
import type { SessionLive } from "../../../state/session-live";
import { useQuery } from "../../../state/query";
import { Button } from "../../../ui/Button";
import { EmptyState } from "../../../ui/EmptyState";
import { ErrorNotice } from "../../../ui/ErrorNotice";
import { Field, Input } from "../../../ui/Field";
import { Skeleton } from "../../../ui/Skeleton";
import { useAction } from "../../../ui/useAction";
import { formatBytes } from "../../../ui/format";

function NotConnected({ session, live }: { session: Session; live: SessionLive }) {
  const { t } = useI18n();
  const api = useApi();
  const activate = useAction(async (key) => {
    await api.sessions.activateExecutor(session.id, { idempotencyKey: key });
    live.refresh(["session", "executor"]);
  });
  return (
    <EmptyState
      icon={<FileIcon size={18} />}
      title={t("files.not_connected")}
      body={t("files.not_connected_body")}
      action={
        <>
          <ErrorNotice error={activate.error} />
          {/* Closed sessions cannot be re-activated (server rejects with invalid_transition). */}
          {session.lifecycle === "closed" ? null : (
            <Button size="sm" variant="secondary" disabled={activate.pending} onClick={() => void activate.run()}>
              {t("session.activate_workspace")}
            </Button>
          )}
        </>
      }
    />
  );
}

/** Directory listing: the worktree as the Executor reports it right now. */
export function FilesTab({ session, live, rev }: { session: Session; live: SessionLive; rev: number }) {
  const { t } = useI18n();
  const api = useApi();
  const [dir, setDir] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [newPath, setNewPath] = useState("");
  const [localRev, setLocalRev] = useState(0);
  const list = useQuery(["files", session.id, dir, rev + localRev], () => api.files.list(session.id, dir));
  const file = useQuery(open ? ["file", session.id, open, rev + localRev] : null, () => api.files.read(session.id, open!));
  const unavailable = isApiError(list.error) && list.error.code === "executor_unavailable";
  const entries = [...(list.data?.items ?? [])].sort(
    (a, b) => Number(b.type === "dir") - Number(a.type === "dir") || a.path.localeCompare(b.path),
  );
  const crumbs = dir ? dir.split("/") : [];

  if (unavailable) return <NotConnected session={session} live={live} />;
  if (open) {
    return (
      <Editor
        key={`${open}:${file.data?.digest ?? ""}`}
        sessionId={session.id}
        path={open}
        file={file.data}
        // Only a definite not_found opens a blank editor; any other error keeps its own recovery.
        missing={isApiError(file.error) && file.error.code === "not_found"}
        loading={file.loading}
        onSaved={() => setLocalRev(localRev + 1)}
        onReload={() => setLocalRev(localRev + 1)}
        onClose={() => setOpen(null)}
        error={file.error}
      />
    );
  }
  return (
    <div className="files">
      <nav className="files__crumbs" aria-label={t("tab.files")}>
        <button
          type="button"
          className={`files__crumb${!dir ? " files__crumb--current" : ""}`}
          aria-current={!dir ? "page" : undefined}
          onClick={() => setDir("")}
        >
          /
        </button>
        {crumbs.map((seg, i) => (
          <span key={i} className="files__crumbseg">
            <span className="faint">/</span>
            <button
              type="button"
              className={`files__crumb${i === crumbs.length - 1 ? " files__crumb--current" : ""}`}
              aria-current={i === crumbs.length - 1 ? "page" : undefined}
              onClick={() => setDir(crumbs.slice(0, i + 1).join("/"))}
            >
              {seg}
            </button>
          </span>
        ))}
        <span className="files__spacer" />
        <Button size="sm" variant="ghost" icon={<RotateCw size={12} />} onClick={() => setLocalRev(localRev + 1)} title={t("common.refresh")}>
          {t("common.refresh")}
        </Button>
      </nav>
      {list.loading && !list.data ? <Skeleton height={66} /> : null}
      <ErrorNotice error={list.error} onRetry={list.refetch} />
      {list.data ? (
        entries.length ? (
          <ul className="files__list" aria-label={t("tab.files")}>
            {entries.map((e) => (
              <li key={e.path}>
                <button
                  type="button"
                  className="files__row"
                  onClick={() => (e.type === "dir" ? setDir(e.path) : setOpen(e.path))}
                >
                  {e.type === "dir" ? <Folder size={14} className="faint" /> : <FileIcon size={14} className="faint" />}
                  <span className="files__name truncate">{e.path.split("/").pop()}</span>
                  <span className="files__size faint">{e.type === "file" && e.size != null ? formatBytes(e.size) : ""}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState icon={<Folder size={18} />} title={t("files.empty", { path: `/${dir || ""}` })} />
        )
      ) : null}
      <form
        className="files__new"
        onSubmit={(e) => {
          e.preventDefault();
          if (newPath.trim()) setOpen(newPath.trim());
        }}
      >
        <Field label={t("files.new")} htmlFor="new-file">
          <Input id="new-file" value={newPath} onChange={(e) => setNewPath(e.target.value)} placeholder="path/to/file" />
        </Field>
        <Button type="submit" size="sm" variant="secondary">
          {t("files.open")}
        </Button>
      </form>
    </div>
  );
}

/** Why a view cannot be saved back; null when it is the complete text. */
function readOnlyReason(file: FileContent): "binary" | "truncated" | "redacted" | null {
  if (file.encoding === "base64") return "binary";
  if (file.editable === true) return null;
  if (file.editable === false) return file.readonly_reason ?? (file.redacted ? "redacted" : "truncated");
  // Runtimes before the explicit flag: a partial view is still never editable.
  return file.truncated ? "truncated" : file.redacted ? "redacted" : file.digest ? null : "truncated";
}

function Editor({
  sessionId,
  path,
  file,
  missing,
  loading,
  error,
  onSaved,
  onReload,
  onClose,
}: {
  sessionId: string;
  path: string;
  file: FileContent | undefined;
  missing: boolean;
  loading: boolean;
  error: unknown;
  onSaved: () => void;
  onReload: () => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const api = useApi();
  const [text, setText] = useState(file?.content ?? "");
  const [editing, setEditing] = useState(false);
  const readOnly = file ? readOnlyReason(file) : null;
  // A missing path goes straight to a blank editor: that is what "open or create" means.
  const isEditing = (editing || missing) && !readOnly;
  const save = useAction(async (key) => {
    const expected = missing ? "absent" : file?.digest;
    if (!expected) throw new Error(t("files.readonly_partial"));
    await api.files.write(sessionId, { path, content: text, expected_digest: expected }, { idempotencyKey: key });
    setEditing(false);
    onSaved();
  });
  const conflict = isApiError(save.error) && save.error.code === "version_conflict";
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (isEditing) void save.run();
  };
  const lines = file ? file.content.split("\n") : [];
  return (
    <form className="files editor" onSubmit={submit} aria-label={t("files.editor", { path })}>
      <div className="files__viewbar">
        <Button size="sm" variant="ghost" icon={<ArrowLeft size={13} />} onClick={onClose} type="button">
          {path}
        </Button>
        <span className="files__spacer" />
        {file && !readOnly && !isEditing ? (
          <Button size="sm" variant="secondary" icon={<FilePen size={12} />} type="button" onClick={() => setEditing(true)}>
            {t("files.edit")}
          </Button>
        ) : null}
        {isEditing ? (
          <>
            <Button
              size="sm"
              variant="ghost"
              icon={<X size={12} />}
              type="button"
              onClick={() => (missing ? onClose() : (setEditing(false), setText(file?.content ?? "")))}
            >
              {t("files.cancel_edit")}
            </Button>
            <Button size="sm" variant="primary" icon={<Check size={12} />} type="submit" disabled={save.pending}>
              {missing ? t("files.create") : t("files.save")}
            </Button>
          </>
        ) : null}
      </div>
      {loading && !file && !missing ? <Skeleton height={66} /> : null}
      {missing ? <p className="muted small" data-testid="files-new-hint">{t("files.new_hint")}</p> : null}
      {file && readOnly ? (
        <div className="notice notice--warn" role="status" data-testid="files-readonly">
          <div className="notice__body">
            <div className="notice__title">{t(`files.readonly.${readOnly}`)}</div>
            <div className="notice__text">
              {readOnly === "truncated"
                ? t("files.readonly.truncated_body", {
                    // Exact counts: rounded units would print the same number twice.
                    shown: new TextEncoder().encode(file.content).length.toLocaleString(),
                    size: file.size != null ? file.size.toLocaleString() : "?",
                  })
                : t(`files.readonly.${readOnly}_body`)}
            </div>
          </div>
        </div>
      ) : null}
      {conflict ? (
        <div className="notice notice--warn" role="alert">
          <div className="notice__body">
            <div className="notice__title">{t(missing ? "files.conflict_created" : "files.conflict_changed")}</div>
            <div className="notice__text">{t("files.conflict_body")}</div>
          </div>
          <Button size="sm" variant="secondary" type="button" onClick={onReload}>
            {t("files.reload_current")}
          </Button>
        </div>
      ) : null}
      {readOnly === "binary" ? null : isEditing ? (
        <>
          <label className="sr-only" htmlFor="file-editor">
            {path}
          </label>
          <textarea
            id="file-editor"
            className="files__editor mono"
            rows={14}
            spellCheck={false}
            value={text}
            autoFocus={missing}
            onChange={(e) => setText(e.target.value)}
          />
          {conflict ? null : <ErrorNotice error={save.error} onRetry={() => void save.run()} retryLabel={t("files.retry_save")} />}
        </>
      ) : file ? (
        <pre className="files__code" aria-label={t("files.view")}>
          {lines.map((l, i) => (
            <span key={i} className="files__line">
              <span className="files__no">{i + 1}</span>
              {l}
              {"\n"}
            </span>
          ))}
        </pre>
      ) : missing ? null : (
        <ErrorNotice error={error} onRetry={onReload} />
      )}
    </form>
  );
}

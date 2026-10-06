import { useState, type FormEvent } from "react";
import type { FileContent } from "../../api/types";
import { Empty, ErrorNotice, Field, Loading, useAction } from "../../components/ui";
import { useI18n } from "../../i18n";
import { useApi } from "../../state/context";
import { useQuery } from "../../state/query";

/** Bounded runtime observation. Saves carry the digest read; unavailable compute is a diagnosis. */
export function FilesTab({ sessionId }: { sessionId: string }) {
  const { t } = useI18n();
  const api = useApi();
  const [dir, setDir] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [newPath, setNewPath] = useState("");
  const [rev, setRev] = useState(0);
  const list = useQuery(["files", sessionId, dir, rev], () => api.files.list(sessionId, dir));
  const file = useQuery(open ? ["file", sessionId, open, rev] : null, () => api.files.read(sessionId, open!));
  const entries = [...(list.data?.items ?? [])].sort((a, b) => Number(b.type === "dir") - Number(a.type === "dir") || a.path.localeCompare(b.path));
  const parent = dir.includes("/") ? dir.slice(0, dir.lastIndexOf("/")) : "";

  return (
    <div className="files-tab">
      <div className="row wrap small">
        <button type="button" className="btn btn-sm" disabled={!dir} onClick={() => setDir(parent)}>
          ..
        </button>
        <code>/{dir}</code>
        <span className="grow" />
        <button type="button" className="btn btn-sm" onClick={() => setRev(rev + 1)}>
          {t("common.refresh")}
        </button>
      </div>
      {list.loading && !list.data ? <Loading /> : null}
      <ErrorNotice error={list.error} onRetry={list.refetch} />
      {list.data && !entries.length ? <Empty>{t("files.empty")}</Empty> : null}
      <ul className="file-tree" aria-label={t("tab.files")}>
        {entries.map((e) => (
          <li key={e.path}>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => (e.type === "dir" ? setDir(e.path) : setOpen(e.path))}>
              {e.type === "dir" ? "▸ " : ""}
              {e.path.split("/").pop()}
            </button>
          </li>
        ))}
      </ul>
      <form
        className="row wrap"
        onSubmit={(e) => {
          e.preventDefault();
          if (newPath.trim()) setOpen(newPath.trim());
        }}
      >
        <Field id="new-file" label={t("files.new")}>
          <input id="new-file" value={newPath} onChange={(e) => setNewPath(e.target.value)} placeholder="path/to/file" />
        </Field>
        <button type="submit" className="btn btn-sm">
          {t("files.open")}
        </button>
      </form>
      {open ? <Editor key={`${open}:${file.data?.digest ?? ""}`} sessionId={sessionId} path={open} file={file.data} missing={Boolean(file.error)} loading={file.loading} onSaved={() => setRev(rev + 1)} error={file.error} /> : null}
    </div>
  );
}

function Editor({
  sessionId,
  path,
  file,
  missing,
  loading,
  error,
  onSaved,
}: {
  sessionId: string;
  path: string;
  file: FileContent | undefined;
  missing: boolean;
  loading: boolean;
  error: unknown;
  onSaved: () => void;
}) {
  const { t } = useI18n();
  const api = useApi();
  const [text, setText] = useState(file?.content ?? "");
  const binary = file?.encoding === "base64";
  const save = useAction(async (key) => {
    await api.files.write(sessionId, { path, content: text, expected_digest: file?.digest ?? "absent" }, { idempotencyKey: key });
    onSaved();
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void save.run();
  };
  return (
    <form className="editor" onSubmit={submit} aria-label={t("files.editor", { path })}>
      <h3>
        <code>{path}</code>
      </h3>
      {loading && !file ? <Loading /> : null}
      {missing ? <p className="muted small">{t("files.new_hint")}</p> : null}
      {binary ? (
        <p className="muted small">{t("files.binary")}</p>
      ) : (
        <>
          <label className="sr-only" htmlFor="file-editor">
            {path}
          </label>
          <textarea id="file-editor" className="mono" rows={14} spellCheck={false} value={text} onChange={(e) => setText(e.target.value)} />
          <ErrorNotice error={save.error ?? (missing ? null : error)} onRetry={() => void save.run()} retryLabel={t("files.retry_save")} />
          <button type="submit" className="btn btn-primary btn-sm" disabled={save.pending}>
            {t("files.save")}
          </button>
        </>
      )}
    </form>
  );
}

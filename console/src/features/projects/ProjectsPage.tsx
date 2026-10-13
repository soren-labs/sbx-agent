import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { FolderGit2, MoreHorizontal, Plus } from "lucide-react";
import type { Project, ProjectSpec } from "../../api/types";
import { useI18n } from "../../i18n";
import { useAuth } from "../../state/auth";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";
import { useDocumentTitle } from "../../state/title";
import { Button, IconButton } from "../../ui/Button";
import { Dialog } from "../../ui/Dialog";
import { EmptyState } from "../../ui/EmptyState";
import { ErrorNotice } from "../../ui/ErrorNotice";
import { Field, Input, Select, Textarea } from "../../ui/Field";
import { when } from "../../ui/format";
import { Menu, MenuItem } from "../../ui/Menu";
import { Spinner } from "../../ui/Spinner";
import { useAction } from "../../ui/useAction";
import { PageHeader } from "../../shell/PageHeader";

function parseChecks(text: string): { name: string; argv: string[] }[] {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => {
      const i = l.indexOf("=");
      const name = i > 0 ? l.slice(0, i).trim() : l.split(/\s+/)[0];
      const cmd = i > 0 ? l.slice(i + 1).trim() : l;
      return { name, argv: cmd.split(/\s+/) };
    });
}

function checksText(spec: ProjectSpec | undefined): string {
  return (spec?.checks ?? []).map((c) => `${c.name}=${c.argv.join(" ")}`).join("\n");
}

function ProjectForm({
  project,
  onDone,
  onCancel,
}: {
  project?: Project;
  onDone: () => void;
  onCancel?: () => void;
}) {
  const { t } = useI18n();
  const api = useApi();
  const { workspace } = useAuth();
  const spec = project?.current_version?.spec;
  const [slug, setSlug] = useState(project?.slug ?? "");
  const [name, setName] = useState(project?.name ?? "");
  const [repo, setRepo] = useState(spec?.repository.full_name ?? "");
  const [baseRef, setBaseRef] = useState(spec?.repository.base_ref ?? "main");
  const [checks, setChecks] = useState(checksText(spec));
  const [model, setModel] = useState(spec?.defaults?.harness?.model ?? "");
  const [backend, setBackend] = useState(spec?.defaults?.executor?.backend ?? "modal");
  const uid = project?.id ?? "new";

  const save = useAction(async (key) => {
    const next: ProjectSpec = {
      repository: { full_name: repo.trim(), base_ref: baseRef.trim() || "main" },
      checks: parseChecks(checks),
      defaults: {
        harness: { provider_id: "opencode", ...(model.trim() ? { model: model.trim() } : {}) },
        executor: { backend },
      },
    };
    if (project)
      await api.projects.publishVersion(project.id, { spec: next, expected_version: project.version }, { idempotencyKey: key });
    else await api.projects.create(workspace!.id, { slug: slug.trim(), name: name.trim(), spec: next }, { idempotencyKey: key });
    onDone();
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void save.run();
  };
  return (
    <form onSubmit={submit} aria-label={project ? t("projects.new_version") : t("projects.create")}>
      {!project ? (
        <div className="form-grid">
          <Field htmlFor={`${uid}-slug`} label={t("projects.slug")}>
            <Input
              id={`${uid}-slug`}
              required
              pattern="[a-z0-9][a-z0-9-]*"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
            />
          </Field>
          <Field htmlFor={`${uid}-name`} label={t("projects.name")}>
            <Input id={`${uid}-name`} required value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
        </div>
      ) : null}
      <div className="form-grid">
        <Field htmlFor={`${uid}-repo`} label={t("session.repository")}>
          <Input
            id={`${uid}-repo`}
            required
            placeholder="owner/repo"
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
          />
        </Field>
        <Field htmlFor={`${uid}-ref`} label={t("composer.base_ref")}>
          <Input id={`${uid}-ref`} value={baseRef} onChange={(e) => setBaseRef(e.target.value)} />
        </Field>
      </div>
      <Field htmlFor={`${uid}-checks`} label={t("projects.checks")} hint={t("projects.checks_hint")}>
        <Textarea id={`${uid}-checks`} rows={3} className="mono" value={checks} onChange={(e) => setChecks(e.target.value)} />
      </Field>
      <div className="form-grid">
        <Field htmlFor={`${uid}-model`} label={t("composer.model")}>
          <Input id={`${uid}-model`} value={model} onChange={(e) => setModel(e.target.value)} />
        </Field>
        <Field htmlFor={`${uid}-backend`} label={t("composer.compute")}>
          <Select id={`${uid}-backend`} value={backend} onChange={(e) => setBackend(e.target.value)}>
            <option value="modal">Modal</option>
            <option value="local">Local</option>
          </Select>
        </Field>
      </div>
      <ErrorNotice error={save.error} />
      <div className="mc-actions">
        <Button type="submit" variant="primary" disabled={save.pending}>
          {project ? t("projects.publish") : t("projects.create")}
        </Button>
        {onCancel ? (
          <Button variant="ghost" onClick={onCancel}>
            {t("common.cancel")}
          </Button>
        ) : null}
      </div>
    </form>
  );
}

export function ProjectsPage() {
  const { t } = useI18n();
  useDocumentTitle(t("nav.projects"));
  const api = useApi();
  const qc = useQueryClient();
  const nav = useNavigate();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const q = useQuery(w ? ["projects", w] : null, () => api.projects.list(w!));
  const [creating, setCreating] = useState(false);
  const [publishing, setPublishing] = useState<Project | null>(null);
  const reload = () => {
    setCreating(false);
    setPublishing(null);
    qc.invalidate(["projects"]);
  };

  return (
    <div className="page">
      <PageHeader
        title={t("nav.projects")}
        actions={
          <Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={() => setCreating(true)}>
            {t("projects.new_dialog")}
          </Button>
        }
      />
      <p className="page__lead">{t("projects.intro")}</p>
      {q.loading && !q.data ? <Spinner /> : null}
      <ErrorNotice error={q.error} onRetry={q.refetch} />
      {q.data && !q.data.items.length ? (
        <EmptyState title={t("projects.none")} action={<Button onClick={() => setCreating(true)}>{t("projects.new_dialog")}</Button>} />
      ) : null}
      {q.data?.items.length ? (
      <div className="lcard">
        {(q.data?.items ?? []).map((p) => (
          <div key={p.id} className="lrow" role="group" aria-label={p.name}>
            <div className="lrow__line">
              <span className="lrow__tile" aria-hidden="true">
                <FolderGit2 size={16} />
              </span>
              <div className="lrow__main">
                <span className="lrow__name">{p.name}</span>
                <span className="lrow__sub">
                  {p.current_version
                    ? `${p.current_version.spec.repository.full_name}@${p.current_version.spec.repository.base_ref} · ${t(
                        "projects.version_n",
                        { n: p.current_version.ordinal },
                      )} · ${t("projects.updated")} ${when(p.current_version.created_at)}`
                    : t("projects.no_version")}
                </span>
              </div>
              <div className="lrow__aside">
                <Menu
                  align="end"
                  label={t("projects.actions")}
                  trigger={<IconButton size="sm" label={t("projects.actions")} icon={<MoreHorizontal size={15} />} />}
                >
                  <MenuItem label={t("projects.new_version")} onSelect={() => setPublishing(p)} />
                  <MenuItem label={t("projects.start_session")} onSelect={() => nav(`/?project=${p.id}`)} />
                </Menu>
              </div>
            </div>
          </div>
        ))}
      </div>
      ) : null}

      {creating ? (
        <Dialog title={t("projects.new_dialog")} onClose={() => setCreating(false)} width={480}>
          <ProjectForm onDone={reload} onCancel={() => setCreating(false)} />
        </Dialog>
      ) : null}
      {publishing ? (
        <Dialog title={t("projects.publish_title")} onClose={() => setPublishing(null)} width={480}>
          <ProjectForm project={publishing} onDone={reload} onCancel={() => setPublishing(null)} />
        </Dialog>
      ) : null}
    </div>
  );
}

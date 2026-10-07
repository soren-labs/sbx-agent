import { useState, type FormEvent } from "react";
import type { Project, ProjectSpec } from "../../api/types";
import { Empty, ErrorNotice, Field, Loading, useAction, when } from "../../components/ui";
import { useI18n } from "../../i18n";
import { useAuth } from "../../state/auth";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";
import { useDocumentTitle } from "../../state/title";

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
}: {
  project?: Project;
  onDone: () => void;
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
    if (project) await api.projects.publishVersion(project.id, { spec: next, expected_version: project.version }, { idempotencyKey: key });
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
        <div className="controls">
          <Field id={`${uid}-slug`} label={t("projects.slug")}>
            <input id={`${uid}-slug`} required pattern="[a-z0-9][a-z0-9-]*" value={slug} onChange={(e) => setSlug(e.target.value)} />
          </Field>
          <Field id={`${uid}-name`} label={t("projects.name")}>
            <input id={`${uid}-name`} required value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
        </div>
      ) : null}
      <div className="controls">
        <Field id={`${uid}-repo`} label={t("composer.repo")}>
          <input id={`${uid}-repo`} required placeholder="owner/repo" value={repo} onChange={(e) => setRepo(e.target.value)} />
        </Field>
        <Field id={`${uid}-ref`} label={t("composer.base_ref")}>
          <input id={`${uid}-ref`} value={baseRef} onChange={(e) => setBaseRef(e.target.value)} />
        </Field>
      </div>
      <Field id={`${uid}-checks`} label={t("projects.checks")} hint={t("projects.checks_hint")}>
        <textarea id={`${uid}-checks`} rows={3} className="mono" value={checks} onChange={(e) => setChecks(e.target.value)} />
      </Field>
      <div className="controls">
        <Field id={`${uid}-model`} label={t("composer.model")}>
          <input id={`${uid}-model`} value={model} onChange={(e) => setModel(e.target.value)} />
        </Field>
        <Field id={`${uid}-backend`} label={t("composer.executor")}>
          <select id={`${uid}-backend`} value={backend} onChange={(e) => setBackend(e.target.value)}>
            <option value="modal">Modal</option>
            <option value="local">Local</option>
          </select>
        </Field>
      </div>
      <ErrorNotice error={save.error} />
      <button type="submit" className="btn btn-primary btn-sm" disabled={save.pending}>
        {project ? t("projects.publish") : t("projects.create")}
      </button>
    </form>
  );
}

export function ProjectsPage() {
  const { t } = useI18n();
  useDocumentTitle(t("nav.projects"));
  const api = useApi();
  const qc = useQueryClient();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const q = useQuery(w ? ["projects", w] : null, () => api.projects.list(w!));
  const [editing, setEditing] = useState<string | null>(null);
  const reload = () => {
    setEditing(null);
    qc.invalidate(["projects"]);
  };
  return (
    <div className="narrow">
      <h1>{t("nav.projects")}</h1>
      <p className="page-lead muted">{t("projects.intro")}</p>
      {q.loading && !q.data ? <Loading /> : null}
      <ErrorNotice error={q.error} onRetry={q.refetch} />
      {q.data && !q.data.items.length ? <Empty>{t("projects.none")}</Empty> : null}
      <ul className="plain-list">
        {(q.data?.items ?? []).map((p) => (
          <li key={p.id} className="card" aria-label={p.name}>
            <div className="row wrap">
              <strong className="grow">{p.name}</strong>
              <code className="faint">{p.slug}</code>
            </div>
            <p className="small muted">
              {p.current_version
                ? `${p.current_version.spec.repository.full_name}@${p.current_version.spec.repository.base_ref} · v${p.current_version.ordinal} · ${when(p.current_version.created_at)}`
                : t("projects.no_version")}
            </p>
            <button type="button" className="btn btn-sm" aria-expanded={editing === p.id} onClick={() => setEditing(editing === p.id ? null : p.id)}>
              {t("projects.new_version")}
            </button>
            {editing === p.id ? <ProjectForm project={p} onDone={reload} /> : null}
          </li>
        ))}
      </ul>
      <section className="card">
        <h2>{t("projects.create")}</h2>
        <ProjectForm onDone={reload} />
      </section>
    </div>
  );
}

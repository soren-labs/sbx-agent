import { useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { FolderGit2, MoreHorizontal, Plus, Trash2 } from "lucide-react";
import type { Project, ProjectSpec, ShipPolicy } from "../../api/types";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
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
import { harnessName, selectable } from "../sessions/harnesses";
import { ArgvError, joinArgv, parseCommandLines, splitArgv } from "./argv";
import { deriveSpec, editsOf, specChanges, type SpecChange } from "./spec";

type T = ReturnType<typeof useI18n>["t"];

function argvErrorText(t: T, e: ArgvError): string {
  return t(`projects.argv_error.${e.code}` as I18nKey, { col: e.column ?? "" });
}

/** The exact argv a command will run with: one chip per argument, no shell involved. */
function ArgvPreview({ argv, label }: { argv: string[]; label: string }) {
  return (
    <ol className="argv" aria-label={label}>
      {argv.map((a, i) => (
        <li key={i} className={a ? "argv__word mono" : "argv__word argv__word--empty mono"}>
          {a || "''"}
        </li>
      ))}
    </ol>
  );
}

interface ServiceRow {
  key: number;
  name: string;
  command: string;
  port: string;
}

function policyLines(t: T, ship: ShipPolicy | undefined): string[] {
  if (!ship) return [];
  const transport = t(`projects.policy.transport.${ship.transport ?? "pull_request"}` as I18nKey);
  const out = [
    t(ship.draft === false ? "projects.policy.deliver_ready" : "projects.policy.deliver_draft", {
      transport,
      base: ship.base_branch ?? "—",
    }),
  ];
  const results = (ship.required_results ?? []).map((r) => `${r.count ?? 1}× ${r.kind}${r.verdict ? ` (${r.verdict})` : ""}`);
  out.push(t("projects.policy.required", { results: results.length ? results.join(", ") : t("projects.policy.nothing") }));
  if ((ship.required_checks ?? []).length) out.push(t("projects.policy.checks", { checks: (ship.required_checks ?? []).join(", ") }));
  out.push(t("projects.policy.merge", { methods: (ship.merge_methods ?? []).join(", ") || "—" }));
  out.push(t(ship.automatic_delivery ? "projects.policy.automatic_on" : "projects.policy.automatic_off"));
  return out;
}

/** What a new version keeps without showing an input for it. */
function KeptSettings({ spec, title }: { spec: ProjectSpec; title: string }) {
  const { t } = useI18n();
  const env = Object.keys(spec.environment?.env ?? {});
  const effort = spec.defaults?.harness?.effort;
  const known = new Set(["schema_version", "repository", "environment", "checks", "services", "defaults", "ship_policy"]);
  const extra = Object.keys(spec).filter((k) => !known.has(k));
  return (
    <section className="proj-kept" aria-label={title}>
      <h3 className="proj-kept__title">{title}</h3>
      <ul className="proj-kept__list small">
        {effort ? <li>{t("projects.kept.effort", { effort })}</li> : null}
        <li>{env.length ? t("projects.kept.env", { n: env.length, names: env.join(", ") }) : t("projects.kept.env_none")}</li>
        {policyLines(t, spec.ship_policy).map((l) => (
          <li key={l}>{l}</li>
        ))}
        {extra.length ? <li>{t("projects.kept.extra", { names: extra.join(", ") })}</li> : null}
      </ul>
    </section>
  );
}

function ChangeList({ changes }: { changes: SpecChange[] }) {
  const { t } = useI18n();
  return (
    <ul className="proj-diff" aria-label={t("projects.changes")}>
      {changes.map((c) => (
        <li key={c.path} className={c.policy ? "proj-diff__row proj-diff__row--policy" : "proj-diff__row"}>
          <code className="proj-diff__path">{c.path}</code>
          <span className="proj-diff__values mono small">
            <del>{c.before ?? t("projects.diff_absent")}</del> → <ins>{c.after ?? t("projects.diff_absent")}</ins>
          </span>
        </li>
      ))}
    </ul>
  );
}

let rowKey = 0;

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
  const harnesses = useQuery(["harnesses"], () => api.catalog.harnesses());
  const initial = useMemo(() => editsOf(spec), [spec]);
  const [slug, setSlug] = useState(project?.slug ?? "");
  const [name, setName] = useState(project?.name ?? "");
  const [repo, setRepo] = useState(initial.repository);
  const [baseRef, setBaseRef] = useState(initial.baseRef);
  const [checks, setChecks] = useState(initial.checks.map((c) => `${c.name}=${joinArgv(c.argv)}`).join("\n"));
  const [services, setServices] = useState<ServiceRow[]>(() =>
    initial.services.map((s) => ({ key: ++rowKey, name: s.name, command: joinArgv(s.argv), port: s.port ? String(s.port) : "" })),
  );
  const [harness, setHarness] = useState(initial.harness);
  const [model, setModel] = useState(initial.model);
  const [backend, setBackend] = useState(initial.backend);
  const [policyAck, setPolicyAck] = useState(false);
  const uid = project?.id ?? "new";

  const offered = selectable(harnesses.data?.items).map((h) => h.provider_id);
  const harnessOptions = offered.includes(harness) ? offered : [harness, ...offered];

  const checkLines = parseCommandLines(checks);
  const serviceRows = services.map((r) => {
    let argv: string[] | null = null;
    let error: string | null = null;
    try {
      argv = splitArgv(r.command);
      if (!argv.length) error = t("projects.service_command_missing");
      else if (!argv[0]) error = t("projects.argv_error.empty_program");
    } catch (e) {
      if (!(e instanceof ArgvError)) throw e;
      error = argvErrorText(t, e);
    }
    const port = r.port.trim() ? Number(r.port) : null;
    if (port !== null && !(Number.isInteger(port) && port > 0 && port < 65536)) error = error ?? t("projects.service_port_invalid");
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(r.name.trim())) error = error ?? t("projects.service_name_invalid");
    return { row: r, argv, port, error };
  });
  const names = serviceRows.map((s) => s.row.name.trim());
  const duplicate = names.find((n, i) => n && names.indexOf(n) !== i);
  const invalid = checkLines.some((l) => l.error) || serviceRows.some((s) => s.error) || !!duplicate;

  const next = useMemo(
    () =>
      invalid
        ? null
        : deriveSpec(spec, {
            repository: repo,
            baseRef,
            checks: checkLines.map((l) => ({ name: l.parsed!.name, argv: l.parsed!.argv })),
            services: serviceRows.map((s) => ({ name: s.row.name.trim(), argv: s.argv!, port: s.port })),
            harness,
            model,
            backend,
          }),
    [invalid, spec, repo, baseRef, checks, services, harness, model, backend],
  );
  const changes = project && next ? specChanges(spec, next) : [];
  const policyChanged = changes.some((c) => c.policy);
  const unchanged = !!project && !!next && changes.length === 0;

  const save = useAction(async (key) => {
    if (!next) return;
    if (project)
      await api.projects.publishVersion(project.id, { spec: next, expected_version: project.version }, { idempotencyKey: key });
    else await api.projects.create(workspace!.id, { slug: slug.trim(), name: name.trim(), spec: next }, { idempotencyKey: key });
    onDone();
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void save.run();
  };
  const setRow = (key: number, patch: Partial<ServiceRow>) =>
    setServices((rows) => rows.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  const blocked = save.pending || invalid || unchanged || (policyChanged && !policyAck);

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
        <Field htmlFor={`${uid}-repo`} label={t("session.repository")} hint={t("projects.repository_hint")}>
          <Input id={`${uid}-repo`} placeholder="owner/repo" value={repo} onChange={(e) => setRepo(e.target.value)} />
        </Field>
        <Field htmlFor={`${uid}-ref`} label={t("composer.base_ref")}>
          <Input id={`${uid}-ref`} value={baseRef} onChange={(e) => setBaseRef(e.target.value)} />
        </Field>
      </div>
      <div className="form-grid form-grid--3">
        <Field htmlFor={`${uid}-harness`} label={t("projects.harness")}>
          <Select id={`${uid}-harness`} value={harness} onChange={(e) => setHarness(e.target.value)}>
            {harnessOptions.map((h) => (
              <option key={h} value={h}>
                {harnessName(h)}
              </option>
            ))}
          </Select>
        </Field>
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
      <Field htmlFor={`${uid}-checks`} label={t("projects.checks")} hint={t("projects.checks_hint")}>
        <Textarea id={`${uid}-checks`} rows={3} className="mono" value={checks} onChange={(e) => setChecks(e.target.value)} />
      </Field>
      {checkLines.length ? (
        <ul className="proj-argv-list" aria-label={t("projects.checks_preview")}>
          {checkLines.map((l) => (
            <li key={l.line}>
              {l.parsed ? (
                <>
                  <span className="proj-argv-list__name">{l.parsed.name}</span>
                  <ArgvPreview argv={l.parsed.argv} label={t("projects.argv_of", { name: l.parsed.name })} />
                  {l.parsed.operators.length ? (
                    <p className="proj-argv-list__warn small" role="note">
                      {t("projects.operators_literal", { ops: l.parsed.operators.join(" ") })}
                    </p>
                  ) : null}
                </>
              ) : (
                <p className="field__error small" role="alert">
                  {t("projects.line_error", { line: l.line, error: argvErrorText(t, l.error!) })}
                </p>
              )}
            </li>
          ))}
        </ul>
      ) : null}

      <fieldset className="proj-services">
        <legend>{t("projects.services")}</legend>
        <p className="faint small">{t("projects.services_hint")}</p>
        {serviceRows.map(({ row, argv, error }) => (
          <div key={row.key} className="proj-service" role="group" aria-label={t("projects.service_n", { name: row.name || "…" })}>
            <div className="proj-service__grid">
              <Field htmlFor={`${uid}-svc-${row.key}-name`} label={t("projects.service_name")}>
                <Input id={`${uid}-svc-${row.key}-name`} value={row.name} onChange={(e) => setRow(row.key, { name: e.target.value })} />
              </Field>
              <Field htmlFor={`${uid}-svc-${row.key}-cmd`} label={t("projects.service_command")}>
                <Input
                  id={`${uid}-svc-${row.key}-cmd`}
                  className="mono"
                  value={row.command}
                  onChange={(e) => setRow(row.key, { command: e.target.value })}
                />
              </Field>
              <Field htmlFor={`${uid}-svc-${row.key}-port`} label={t("projects.service_port")}>
                <Input
                  id={`${uid}-svc-${row.key}-port`}
                  inputMode="numeric"
                  value={row.port}
                  onChange={(e) => setRow(row.key, { port: e.target.value })}
                />
              </Field>
              <IconButton
                size="sm"
                label={t("projects.service_remove", { name: row.name || "…" })}
                icon={<Trash2 size={14} />}
                onClick={() => setServices((rows) => rows.filter((r) => r.key !== row.key))}
              />
            </div>
            {error ? (
              <p className="field__error small" role="alert">
                {error}
              </p>
            ) : argv ? (
              <ArgvPreview argv={argv} label={t("projects.argv_of", { name: row.name })} />
            ) : null}
          </div>
        ))}
        {duplicate ? (
          <p className="field__error small" role="alert">
            {t("projects.service_duplicate", { name: duplicate })}
          </p>
        ) : null}
        <div>
          <Button
            size="sm"
            variant="secondary"
            icon={<Plus size={13} />}
            onClick={() => setServices((rows) => [...rows, { key: ++rowKey, name: "", command: "", port: "" }])}
          >
            {t("projects.service_add")}
          </Button>
        </div>
      </fieldset>

      {spec ? <KeptSettings spec={spec} title={t("projects.kept_title", { n: project?.current_version?.ordinal ?? 1 })} /> : null}
      {!project ? <p className="faint small">{t("projects.defaults_policy")}</p> : null}

      {project && next ? (
        <section className="proj-changes" aria-label={t("projects.changes")}>
          <h3 className="proj-kept__title">{t("projects.changes")}</h3>
          {changes.length ? <ChangeList changes={changes} /> : <p className="muted small">{t("projects.no_changes")}</p>}
          {policyChanged ? (
            <label className="small check proj-changes__ack">
              <input type="checkbox" checked={policyAck} onChange={(e) => setPolicyAck(e.target.checked)} />
              {t("projects.policy_ack")}
            </label>
          ) : null}
        </section>
      ) : null}
      <ErrorNotice error={save.error} />
      <div className="mc-actions">
        <Button type="submit" variant="primary" disabled={blocked}>
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
                    ? `${
                        p.current_version.spec.repository
                          ? `${p.current_version.spec.repository.full_name}@${p.current_version.spec.repository.base_ref}`
                          : t("projects.no_repository")
                      } · ${t("projects.version_n", { n: p.current_version.ordinal })} · ${t("projects.updated")} ${when(
                        p.current_version.created_at,
                      )}`
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
        <Dialog title={t("projects.new_dialog")} onClose={() => setCreating(false)} width={640}>
          <ProjectForm onDone={reload} onCancel={() => setCreating(false)} />
        </Dialog>
      ) : null}
      {publishing ? (
        <Dialog title={t("projects.publish_title")} onClose={() => setPublishing(null)} width={640}>
          <ProjectForm project={publishing} onDone={reload} onCancel={() => setPublishing(null)} />
        </Dialog>
      ) : null}
    </div>
  );
}

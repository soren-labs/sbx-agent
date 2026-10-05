import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ErrorNotice, Field, useAction } from "../../components/ui";
import { useI18n } from "../../i18n";
import { useAuth } from "../../state/auth";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";
import { useConnections } from "../connections/ConnectionsPage";
import { modelOptions, pickBackend, pickDefaultModel } from "./defaults";

/** Starts a Session: opencode + preferred free model + Modal by default; Codex is never needed. */
export function NewSession() {
  const { t } = useI18n();
  const api = useApi();
  const qc = useQueryClient();
  const nav = useNavigate();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const connections = useConnections();
  const models = useQuery(w ? ["models", w, "opencode"] : null, () => api.catalog.models(w!, "opencode"));
  const projects = useQuery(w ? ["projects", w] : null, () => api.projects.list(w!));

  const [prompt, setPrompt] = useState("");
  const [projectId, setProjectId] = useState("");
  const [repo, setRepo] = useState("");
  const [baseRef, setBaseRef] = useState("main");
  const [modelOverride, setModelOverride] = useState<string | null>(null);
  const [backendOverride, setBackendOverride] = useState<string | null>(null);

  const options = modelOptions(models.data);
  const model = modelOverride ?? pickDefaultModel(models.data) ?? "";
  const backend = backendOverride ?? pickBackend(connections.data?.items);

  const create = useAction(async (key) => {
    const body = {
      harness: { provider_id: "opencode", ...(model ? { model } : {}) },
      executor: { backend },
      ...(projectId ? { project_id: projectId } : repo.trim() ? { repository: { full_name: repo.trim(), base_ref: baseRef.trim() || "main" } } : {}),
      ...(prompt.trim() ? { message: { content: prompt.trim() } } : {}),
    };
    const r = await api.sessions.create(w!, body, { idempotencyKey: key });
    qc.invalidate(["sessions", w]);
    nav(`/sessions/${r.session_id}`);
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!prompt.trim() || !w) return;
    void create.run();
  };

  return (
    <form className="card composer" onSubmit={submit} aria-label={t("composer.heading")}>
      <h2>{t("composer.heading")}</h2>
      <Field id="new-prompt" label={t("composer.prompt")}>
        <textarea
          id="new-prompt"
          rows={4}
          placeholder={t("composer.prompt_ph")}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit(e);
          }}
        />
      </Field>
      <div className="controls">
        <Field id="new-project" label={t("composer.project")}>
          <select id="new-project" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
            <option value="">{t("composer.no_project")}</option>
            {(projects.data?.items ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </Field>
        {!projectId ? (
          <>
            <Field id="new-repo" label={t("composer.repo")}>
              <input id="new-repo" placeholder="owner/repo" value={repo} onChange={(e) => setRepo(e.target.value)} />
            </Field>
            <Field id="new-ref" label={t("composer.base_ref")}>
              <input id="new-ref" value={baseRef} onChange={(e) => setBaseRef(e.target.value)} />
            </Field>
          </>
        ) : null}
      </div>
      <div className="controls">
        <Field id="new-harness" label={t("composer.harness")}>
          <select id="new-harness" value="opencode" disabled>
            <option value="opencode">opencode</option>
          </select>
        </Field>
        <Field id="new-model" label={t("composer.model")}>
          <select id="new-model" value={model} onChange={(e) => setModelOverride(e.target.value)}>
            {model === "" ? <option value="">{t("composer.model_server")}</option> : null}
            {model !== "" && !options.some((o) => o.id === model) ? <option value={model}>{model}</option> : null}
            {options.map((o) => (
              <option key={o.id} value={o.id}>
                {o.id}
                {o.free ? ` (${t("conn.free")})` : ""}
              </option>
            ))}
          </select>
        </Field>
        <Field id="new-backend" label={t("composer.executor")}>
          <select id="new-backend" value={backend} onChange={(e) => setBackendOverride(e.target.value)}>
            <option value="modal">Modal</option>
            <option value="local">Local</option>
          </select>
        </Field>
      </div>
      <ErrorNotice error={create.error} onRetry={() => void create.run()} retryLabel={t("composer.retry_create")} />
      <div className="send-row">
        <span className="faint small kbd-hint">{t("composer.send_hint")}</span>
        <button type="submit" className="btn btn-primary" disabled={!prompt.trim() || create.pending || !w}>
          {create.pending ? t("composer.starting") : t("composer.start")}
        </button>
      </div>
    </form>
  );
}

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ErrorNotice, useAction } from "../../components/ui";
import { Icon } from "../../components/icons";
import { useI18n } from "../../i18n";
import type { I18nKey } from "../../i18n/en";
import { useAuth } from "../../state/auth";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";
import { useConnections } from "../connections/ConnectionsPage";
import { knownRepositories, modelOptions, pickBackend, pickDefaultModel, pickHarness, usableConnections } from "./defaults";
import { harnessName, PROTOCOL_NAMES, selectable } from "./harnesses";

const shortcutModifier = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl";

export const suggestions: { icon: string; label: I18nKey; prompt: I18nKey }[] = [
  { icon: "code", label: "composer.suggest.bug", prompt: "composer.suggest.bug_prompt" },
  { icon: "check", label: "composer.suggest.tests", prompt: "composer.suggest.tests_prompt" },
  { icon: "refresh", label: "composer.suggest.refactor", prompt: "composer.suggest.refactor_prompt" },
  { icon: "file", label: "composer.suggest.docs", prompt: "composer.suggest.docs_prompt" },
];

export function NewSession({ onPromptChange }: { onPromptChange?: (prompt: string) => void }) {
  const { t } = useI18n();
  const api = useApi();
  const qc = useQueryClient();
  const nav = useNavigate();
  const { workspace } = useAuth();
  const w = workspace?.id ?? null;
  const connections = useConnections();
  const harnesses = useQuery(["harnesses"], () => api.catalog.harnesses());
  const backends = useQuery(["executor-backends"], () => api.catalog.executorBackends());
  const projects = useQuery(w ? ["projects", w] : null, () => api.projects.list(w!));

  const [prompt, setPrompt] = useState("");
  const [projectId, setProjectId] = useState("");
  const [repo, setRepo] = useState("");
  const [baseRef, setBaseRef] = useState("main");
  const [harnessOverride, setHarnessOverride] = useState<string | null>(null);
  const [connectionId, setConnectionId] = useState("");
  const [modelOverride, setModelOverride] = useState<string | null>(null);
  const [backendOverride, setBackendOverride] = useState<string | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const formRef = useRef<HTMLFormElement>(null);

  const offered = selectable(harnesses.data?.items);
  const harness = harnessOverride ?? pickHarness(harnesses.data?.items, connections.data?.items);
  const manifest = offered.find((h) => h.provider_id === harness);
  // Models and compatibility are scoped to the selected Harness: the same key may
  // serve one CLI and not another, depending on the protocols it offers.
  const models = useQuery(w ? ["models", w, harness] : null, () => api.catalog.models(w!, harness));
  const usable = usableConnections(models.data);
  const pinned = usable.some((c) => c.connection_id === connectionId) ? connectionId : "";
  const options = modelOptions(models.data, pinned || undefined);
  const model = modelOverride ?? pickDefaultModel(models.data, pinned || undefined) ?? "";
  const backendKinds = (backends.data?.items ?? []).map((b) => b.kind);
  const backend = backendOverride ?? pickBackend(connections.data?.items, backends.data?.items);
  const repos = knownRepositories(
    connections.data?.items,
    (projects.data?.items ?? []).flatMap((p) => (p.current_version ? [p.current_version.spec.repository.full_name] : [])),
  );
  const blocked = models.data !== undefined && usable.length === 0;

  const create = useAction(async (key) => {
    // The first line of the task names the Session, so lists never show "Untitled".
    const firstLine = prompt.trim().split("\n")[0].trim();
    const body = {
      ...(firstLine ? { title: firstLine.length > 80 ? `${firstLine.slice(0, 79)}…` : firstLine } : {}),
      harness: { provider_id: harness, ...(model ? { model } : {}) },
      executor: { backend },
      ...(pinned ? { connections: { inference: pinned } } : {}),
      ...(projectId
        ? { project_id: projectId }
        : repo.trim()
          ? { repository: { full_name: repo.trim(), base_ref: baseRef.trim() || "main" } }
          : {}),
      ...(prompt.trim() ? { message: { content: prompt.trim() } } : {}),
    };
    const r = await api.sessions.create(w!, body, { idempotencyKey: key });
    qc.invalidate(["sessions", w]);
    nav(`/sessions/${r.session_id}`);
  });

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (!prompt.trim() || !w || create.pending) return;
    void create.run();
  };

  const closePickers = () => {
    formRef.current?.querySelectorAll("details[open]").forEach((d) => d.removeAttribute("open"));
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closePickers();
    };
    // A click outside an open popover dismisses it, like any menu.
    const onPointer = (event: MouseEvent) => {
      formRef.current?.querySelectorAll("details[open]").forEach((d) => {
        if (!d.contains(event.target as Node)) d.removeAttribute("open");
      });
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, []);

  const chooseHarness = (next: string) => {
    setHarnessOverride(next);
    // The model list belongs to the Harness's compatible Connections.
    setModelOverride(null);
    setConnectionId("");
  };

  return (
    <>
      <form ref={formRef} className="card composer session-composer" onSubmit={submit} aria-label={t("composer.heading")}>
        <textarea
          ref={textareaRef}
          id="new-prompt"
          aria-label={t("composer.task")}
          rows={4}
          placeholder={t("composer.task_ph")}
          value={prompt}
          onChange={(e) => {
            setPrompt(e.target.value);
            onPromptChange?.(e.target.value);
          }}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              submit();
            }
          }}
        />

        <div className="composer-tools">
          <details className="composer-picker">
            <summary title={t("composer.select_repo")} className={`repo-chip ${repo ? "selected" : ""}`}>
              <Icon name="github" size={14} />
              <span className="repo-chip-label">{repo || t("composer.select_repo")}</span>
              <Icon name="down" size={11} />
            </summary>
            <div className="picker-popover">
              <h2>{t("composer.select_repo")}</h2>
              <input
                aria-label={t("composer.select_repo")}
                placeholder={t("composer.repo_ph")}
                spellCheck={false}
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
              />
              {repos.length ? (
                <>
                  <p className="faint small">{t("composer.repo_known")}</p>
                  <div className="picker-options">
                    {repos.map((r) => (
                      <button
                        type="button"
                        className="picker-option"
                        key={r}
                        onClick={() => {
                          setRepo(r);
                          closePickers();
                        }}
                      >
                        <Icon name="github" size={13} />
                        <span>{r}</span>
                        {repo === r && <Icon name="check" size={12} />}
                      </button>
                    ))}
                  </div>
                </>
              ) : null}
              <div className="picker-actions">
                {repo ? (
                  <button type="button" className="btn btn-sm btn-ghost" onClick={() => setRepo("")}>
                    {t("composer.repo_none")}
                  </button>
                ) : null}
                <button type="button" className="btn btn-sm btn-ghost" onClick={closePickers}>
                  {t("composer.done")}
                </button>
              </div>
            </div>
          </details>

          <details className="composer-picker adv-toggle session-options">
            <summary title={t("composer.options")} className="config-chip">
              <Icon name="settings" size={13} />
              <span className="composer-configuration-label">
                <span>{t("composer.options")}</span>{" "}
                <span className="faint">· {t(`composer.backend.${backend}` as I18nKey)}</span>
              </span>
            </summary>
            <div className="picker-popover config-picker">
              <h2>{t("composer.configuration")}</h2>

              <label className="form-label" htmlFor="new-harness">
                {t("composer.harness")}
                <select id="new-harness" value={harness} onChange={(e) => chooseHarness(e.target.value)}>
                  {!offered.some((h) => h.provider_id === harness) && <option value={harness}>{harnessName(harness)}</option>}
                  {offered.map((h) => (
                    <option key={h.provider_id} value={h.provider_id}>
                      {harnessName(h.provider_id)}
                    </option>
                  ))}
                </select>
              </label>

              {usable.length > 1 ? (
                <label className="form-label" htmlFor="new-connection">
                  {t("composer.connection")}
                  <select
                    id="new-connection"
                    value={pinned}
                    onChange={(e) => {
                      setConnectionId(e.target.value);
                      setModelOverride(null);
                    }}
                  >
                    <option value="">{t("composer.connection_auto")}</option>
                    {usable.map((c) => (
                      <option key={c.connection_id} value={c.connection_id}>
                        {c.label}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}

              <label className="form-label" htmlFor="new-model">
                {t("composer.model")}
                <select id="new-model" value={model} onChange={(e) => setModelOverride(e.target.value)}>
                  {model === "" && <option value="">{t("composer.model_server")}</option>}
                  {model !== "" && !options.includes(model) && <option value={model}>{model}</option>}
                  {options.map((id) => (
                    <option key={id} value={id}>
                      {id}
                    </option>
                  ))}
                </select>
              </label>

              <label className="form-label" htmlFor="new-backend">
                {t("composer.executor")}
                <select id="new-backend" value={backend} onChange={(e) => setBackendOverride(e.target.value)}>
                  {(backendKinds.length ? backendKinds : ["modal", "local"]).map((kind) => (
                    <option key={kind} value={kind}>
                      {kind === "modal" || kind === "local" ? t(`composer.backend.${kind}` as I18nKey) : kind}
                    </option>
                  ))}
                </select>
              </label>

              <label className="form-label" htmlFor="new-project">
                {t("composer.project")}
                <select id="new-project" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
                  <option value="">{t("composer.no_project")}</option>
                  {(projects.data?.items ?? []).map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>

              <label className="form-label" htmlFor="new-repo">
                {t("composer.repo")}
                <input
                  id="new-repo"
                  placeholder={t("composer.repo_ph")}
                  spellCheck={false}
                  disabled={projectId !== ""}
                  value={repo}
                  onChange={(e) => setRepo(e.target.value)}
                />
              </label>

              <label className="form-label" htmlFor="new-ref">
                {t("composer.base_ref")}
                <input
                  id="new-ref"
                  spellCheck={false}
                  disabled={projectId !== ""}
                  value={baseRef}
                  onChange={(e) => setBaseRef(e.target.value)}
                />
              </label>

              <button type="button" className="btn btn-sm btn-ghost" onClick={closePickers}>
                {t("composer.done")}
              </button>
            </div>
          </details>

          <details className="composer-picker model-picker">
            <summary title={t("composer.agent_model")} data-testid="agent-model-chip">
              <span>
                {harnessName(harness)}
                <span className="faint"> · {model || t("composer.no_model")}</span>
              </span>
              <Icon name="down" size={11} />
            </summary>
            <div className="picker-popover model-popover">
              <h2>{t("composer.harness_label")}</h2>
              <div className="picker-options">
                {offered.map((h) => (
                  <button
                    type="button"
                    className="picker-option"
                    key={h.provider_id}
                    aria-pressed={h.provider_id === harness}
                    onClick={() => chooseHarness(h.provider_id)}
                  >
                    <Icon name="terminal" size={13} />
                    <span>{harnessName(h.provider_id)}</span>
                    {h.provider_id === harness && <Icon name="check" size={12} />}
                  </button>
                ))}
              </div>
              <h2>{t("composer.model")}</h2>
              <div className="picker-options">
                {options.map((id) => (
                  <button
                    type="button"
                    className="picker-option"
                    key={id}
                    aria-pressed={id === model}
                    onClick={() => {
                      setModelOverride(id);
                      closePickers();
                    }}
                  >
                    <Icon name="sparkle" size={13} />
                    <span>{id}</span>
                    {id === model && <Icon name="check" size={12} />}
                  </button>
                ))}
              </div>
              <input
                aria-label={t("composer.custom_model")}
                placeholder={t("composer.custom_model")}
                spellCheck={false}
                value={modelOverride ?? ""}
                onChange={(e) => setModelOverride(e.target.value.trim() === "" ? null : e.target.value.trim())}
              />
              <button type="button" className="btn btn-sm btn-ghost" onClick={closePickers}>
                {t("composer.done")}
              </button>
            </div>
          </details>

          <button
            type="submit"
            className="start-session-button"
            aria-label={t("composer.start")}
            title={t("composer.start_title")}
            disabled={!prompt.trim() || create.pending || !w}
          >
            <Icon name={create.pending ? "clock" : "arrow"} size={16} />
          </button>
        </div>
      </form>

      {blocked ? (
        <p className="hs-note warn composer-hint" role="status">
          {t("composer.needs_inference", {
            harness: harnessName(harness),
            protocols: (manifest?.inference_protocols ?? []).map((p) => PROTOCOL_NAMES[p]).join(" / "),
          })}{" "}
          <Link to="/connections">{t("composer.add_inference")}</Link>
        </p>
      ) : null}

      <ErrorNotice error={create.error} onRetry={() => void create.run()} retryLabel={t("composer.retry_create")} />

      <div className="composer-footnote">
        <span>
          <kbd>{shortcutModifier}</kbd>
          <kbd>Enter</kbd> {t("composer.to_start")}
        </span>
        <span>{t("composer.footnote")}</span>
      </div>

      {!prompt && (
        <div className="suggestion-row" aria-label={t("composer.suggestions")}>
          {suggestions.map((sug) => (
            <button
              type="button"
              key={sug.label}
              className="suggestion-chip"
              onClick={() => {
                const text = t(sug.prompt);
                setPrompt(text);
                onPromptChange?.(text);
                requestAnimationFrame(() => {
                  const el = textareaRef.current;
                  el?.focus();
                  el?.setSelectionRange(text.length, text.length);
                });
              }}
            >
              <Icon name={sug.icon} size={13} />
              <span>{t(sug.label)}</span>
            </button>
          ))}
        </div>
      )}
    </>
  );
}

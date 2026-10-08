import { useEffect, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ErrorNotice, useAction } from "../../components/ui";
import { Icon } from "../../components/icons";
import { useI18n } from "../../i18n";
import { useAuth } from "../../state/auth";
import { useApi, useQueryClient } from "../../state/context";
import { useQuery } from "../../state/query";
import { useConnections } from "../connections/ConnectionsPage";
import { modelOptions, pickBackend, pickDefaultModel } from "./defaults";

const shortcutModifier = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl";

export const suggestions = [
  { icon: "code", label: "Fix a bug", prompt: "Find and fix the bug where " },
  { icon: "check", label: "Add tests", prompt: "Add regression tests covering " },
  { icon: "refresh", label: "Refactor", prompt: "Refactor the following module for readability without changing behaviour: " },
  { icon: "file", label: "Update docs", prompt: "Update the README to document " },
];

export function NewSession({ onPromptChange }: { onPromptChange?: (prompt: string) => void }) {
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
  const [effort, setEffort] = useState("High Effort · Draft PR");

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const formRef = useRef<HTMLFormElement>(null);

  const options = modelOptions(models.data);
  const model = modelOverride ?? pickDefaultModel(models.data) ?? "";
  const backend = backendOverride ?? pickBackend(connections.data?.items);

  const create = useAction(async (key) => {
    const body = {
      harness: { provider_id: "opencode", ...(model ? { model } : {}) },
      executor: { backend },
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

  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        formRef.current?.querySelectorAll("details[open]").forEach((d) => {
          d.removeAttribute("open");
        });
      }
    };
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("keydown", close);
    };
  }, []);

  const closePickers = () => {
    formRef.current?.querySelectorAll("details[open]").forEach((d) => {
      d.removeAttribute("open");
    });
  };

  const modelDisplayName = model
    ? model === "big-pickle"
      ? "GPT-6.1 Sol"
      : model
    : "GPT-6.1 Sol";

  return (
    <>
      <form
        ref={formRef}
        className="card composer session-composer"
        onSubmit={submit}
        aria-label={t("composer.heading")}
      >
        <textarea
          ref={textareaRef}
          id="new-prompt"
          aria-label="Task"
          rows={4}
          placeholder="Describe the work you want to hand off…"
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
          {/* Repo selector dropdown */}
          <details className="composer-picker">
            <summary title="Select repository" className={`repo-chip ${repo ? "selected" : ""}`}>
              <Icon name="github" size={14} />
              <span className="repo-chip-label">{repo || "Select repository"}</span>
              <Icon name="down" size={11} />
            </summary>
            <div className="picker-popover">
              <h2>Select repository</h2>
              <div className="picker-options">
                {["soren-labs/sbx-agent", "soren-labs/docs", "soren-labs/website"].map((r) => (
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
              <button type="button" className="btn btn-sm btn-ghost" onClick={closePickers}>
                Done
              </button>
            </div>
          </details>

          {/* Session configuration / Options popover */}
          <details className="composer-picker adv-toggle session-options">
            <summary title="Session options" className="config-chip">
              <Icon name="settings" size={13} />
              <span className="composer-configuration-label">
                <span>Session options</span> <span className="faint">· {effort}</span>
              </span>
            </summary>
            <div className="picker-popover config-picker">
              <h2>Session configuration</h2>

              <label className="form-label" htmlFor="new-repo">
                Repository
                <input
                  id="new-repo"
                  aria-label="Repository"
                  placeholder="owner/repository"
                  value={repo}
                  onChange={(e) => setRepo(e.target.value)}
                />
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

              <label className="form-label" htmlFor="new-effort">
                Reasoning effort & delivery
                <select
                  id="new-effort"
                  value={effort}
                  onChange={(e) => setEffort(e.target.value)}
                >
                  <option value="High Effort · Draft PR">High Effort · Draft PR</option>
                  <option value="Medium Effort · PR">Medium Effort · PR</option>
                  <option value="Low Effort · Branch">Low Effort · Branch</option>
                  <option value="Auto Effort · Draft PR">Auto Effort · Draft PR</option>
                </select>
              </label>

              <label className="form-label" htmlFor="new-ref">
                {t("composer.base_ref")}
                <input id="new-ref" aria-label="Base branch" value={baseRef} onChange={(e) => setBaseRef(e.target.value)} />
              </label>

              <label className="form-label" htmlFor="new-harness">
                Harness
                <select id="new-harness" aria-label="Harness" value="opencode" disabled>
                  <option value="opencode">opencode</option>
                </select>
              </label>

              <label className="form-label" htmlFor="new-model">
                Model
                <select
                  id="new-model"
                  aria-label="Model"
                  value={model}
                  onChange={(e) => {
                    setModelOverride(e.target.value);
                  }}
                >
                  {model === "" && <option value="">{t("composer.model_server")}</option>}
                  {model !== "" && !options.some((o) => o.id === model) && (
                    <option value={model}>{model}</option>
                  )}
                  {options.map((o) => (
                    <option key={o.id} value={o.id}>
                      {o.id}
                      {o.free ? ` (${t("conn.free")})` : ""}
                    </option>
                  ))}
                  {options.length === 0 && (
                    <option value="big-pickle">big-pickle (free)</option>
                  )}
                </select>
              </label>

              <label className="form-label" htmlFor="new-backend">
                Compute
                <select id="new-backend" aria-label="Compute" value={backend} onChange={(e) => setBackendOverride(e.target.value)}>
                  <option value="modal">modal</option>
                  <option value="local">local</option>
                </select>
              </label>

              <button type="button" className="btn btn-sm btn-ghost" onClick={closePickers}>
                Done
              </button>
            </div>
          </details>

          {/* Model picker popover */}
          <details className="composer-picker model-picker">
            <summary title="Select model">
              <span>{modelDisplayName}</span>
              <Icon name="down" size={11} />
            </summary>
            <div className="picker-popover model-popover">
              <h2>Agent & model</h2>
              <div className="picker-options">
                {["GPT-6.1 Sol", "Claude 3.7 Sonnet", "o3-mini"].map((mName) => (
                  <button
                    type="button"
                    className="picker-option"
                    key={mName}
                    onClick={() => {
                      setModelOverride(mName);
                      closePickers();
                    }}
                  >
                    <Icon name="sparkle" size={13} />
                    <span>{mName}</span>
                    {modelDisplayName === mName && <Icon name="check" size={12} />}
                  </button>
                ))}
              </div>
              <button type="button" className="btn btn-sm btn-ghost" onClick={closePickers}>
                Done
              </button>
            </div>
          </details>

          {/* Circular send affordance button matching image(9) */}
          <button
            type="submit"
            className="start-session-button"
            aria-label="Start session"
            title="Start session · Ctrl/⌘ Enter"
            disabled={!prompt.trim() || create.pending || !w}
          >
            <Icon name={create.pending ? "clock" : "arrow"} size={16} />
          </button>
        </div>
      </form>

      <ErrorNotice error={create.error} onRetry={() => void create.run()} retryLabel={t("composer.retry_create")} />

      <div className="composer-footnote">
        <span>
          <kbd>{shortcutModifier}</kbd>
          <kbd>Enter</kbd> to start
        </span>
        <span>Agents work in an isolated sandbox and never push without your delivery choice.</span>
      </div>

      {!prompt && (
        <div className="suggestion-row" aria-label="Suggestions">
          {suggestions.map((sug) => (
            <button
              type="button"
              key={sug.label}
              className="suggestion-chip"
              onClick={() => {
                setPrompt(sug.prompt);
                onPromptChange?.(sug.prompt);
                requestAnimationFrame(() => {
                  const el = textareaRef.current;
                  el?.focus();
                  el?.setSelectionRange(sug.prompt.length, sug.prompt.length);
                });
              }}
            >
              <Icon name={sug.icon} size={13} />
              <span>{sug.label}</span>
            </button>
          ))}
        </div>
      )}
    </>
  );
}

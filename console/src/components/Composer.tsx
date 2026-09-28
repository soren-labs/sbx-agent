import { useMemo, useState, type FormEvent } from "react";
import { isApiError, type ApiError } from "../api";
import type {
  DeliveryMode,
  EffortLevel,
  ModelInfo,
  NewSessionInput,
  ProviderInfo,
} from "../api/types";
import { useI18n } from "../i18n";
import { loadDefaults, type SessionDefaults } from "../state/prefs";
import { ErrorNotice } from "./ErrorNotice";
import { Icon, Spinner } from "./icons";

const ALL_EFFORTS: EffortLevel[] = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
];

export interface ComposerProps {
  providers: ProviderInfo[];
  /** /v1/models rows — the source for real account ids and per-model
   * effort options. Empty in fixture-less contexts. */
  models?: ModelInfo[];
  submitting: boolean;
  defaults?: SessionDefaults;
  onSubmit: (input: NewSessionInput) => Promise<void> | void;
  onError?: (e: unknown) => void;
}

/**
 * New Session composer: prompt, repo, provider/model (Auto), Send.
 * Everything else lives behind the Advanced disclosure. The account picker
 * only ever offers real account ids (from /v1/models rows for the chosen
 * provider) plus literal "auto" — it cannot fabricate ids.
 */
export function Composer({
  providers,
  models: modelRows = [],
  submitting,
  defaults: defaultsProp,
  onSubmit,
  onError,
}: ComposerProps) {
  const { t } = useI18n();
  const defaults = useMemo(() => defaultsProp ?? loadDefaults(), [defaultsProp]);
  const [prompt, setPrompt] = useState("");
  const [repo, setRepo] = useState("");
  const [repoRef, setRepoRef] = useState("");
  const [provider, setProvider] = useState<string>("auto");
  const [model, setModel] = useState<string>("auto");
  const [advOpen, setAdvOpen] = useState(false);
  const [effort, setEffort] = useState<string>(defaults.effort);
  const [account, setAccount] = useState("auto");
  const [delivery, setDelivery] = useState<DeliveryMode>(defaults.delivery);
  const [deliveryTarget, setDeliveryTarget] = useState("");
  const [cpu, setCpu] = useState("");
  const [mem, setMem] = useState("");
  const [secrets, setSecrets] = useState("");
  const [mcp, setMcp] = useState("");
  const [idleTimeout, setIdleTimeout] = useState<string>(
    defaults.idleTimeoutS === "" ? "" : String(defaults.idleTimeoutS),
  );
  const [error, setError] = useState<ApiError | null>(null);
  const [validation, setValidation] = useState("");

  const modelOptions = useMemo(() => {
    if (provider === "auto") {
      return [...new Set(providers.flatMap((p) => p.models))];
    }
    return providers.find((p) => p.id === provider)?.models ?? [];
  }, [provider, providers]);

  /** Real account ids for the chosen provider — from /v1/models rows only.
   * Never fabricated: absent rows mean the only option is Auto. */
  const accountOptions = useMemo(() => {
    const ids = new Set<string>();
    for (const m of modelRows) {
      if ((provider === "auto" || m.provider === provider) && m.account) {
        ids.add(m.account);
      }
    }
    return [...ids].sort();
  }, [modelRows, provider]);

  /** Effort options for the selected model, or the full catalog on Auto. */
  const effortOptions = useMemo(() => {
    if (model !== "auto") {
      const row = modelRows.find((m) => m.model === model);
      if (row?.reasoningEfforts?.length) return row.reasoningEfforts;
    }
    if (provider !== "auto") {
      const merged = new Set<EffortLevel>();
      for (const m of modelRows) {
        if (m.provider === provider) {
          for (const e of m.reasoningEfforts ?? []) merged.add(e);
        }
      }
      if (merged.size) return [...merged];
    }
    return ALL_EFFORTS;
  }, [model, modelRows, provider]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setValidation("");
    if (!prompt.trim()) {
      setValidation(t("composer.prompt_required"));
      return;
    }
    const input: NewSessionInput = {
      prompt: prompt.trim(),
      repo: repo.trim() || undefined,
      repoRef: repoRef.trim() || undefined,
      provider: provider === "auto" ? "auto" : (provider as NewSessionInput["provider"]),
      model: model === "auto" ? undefined : model,
      effort: effort === "auto" ? undefined : (effort as EffortLevel),
      account: account === "auto" ? undefined : account,
      delivery,
      deliveryTarget:
        delivery === "pr" || delivery === "draft_pr"
          ? deliveryTarget.trim() || undefined
          : undefined,
      compute:
        cpu || mem
          ? {
              cpu: cpu ? Number(cpu) : undefined,
              memoryMib: mem ? Number(mem) : undefined,
            }
          : undefined,
      secrets: secrets
        ? secrets.split(",").map((s) => s.trim()).filter(Boolean)
        : undefined,
      mcpServers: mcp
        ? mcp.split(",").map((s) => s.trim()).filter(Boolean)
        : undefined,
      idleTimeoutS: idleTimeout ? Number(idleTimeout) : undefined,
    };
    try {
      await onSubmit(input);
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setError(apiErr);
      onError?.(err);
    }
  };

  return (
    <form className="composer card" onSubmit={submit} data-testid="composer">
      <h2>{t("composer.heading")}</h2>
      <div className="field">
        <label htmlFor="composer-prompt" className="sr-only">
          {t("composer.heading")}
        </label>
        <textarea
          id="composer-prompt"
          placeholder={t("composer.prompt_ph")}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          disabled={submitting}
          data-testid="composer-prompt"
        />
      </div>
      <div className="controls">
        <div className="field">
          <label htmlFor="composer-repo">{t("composer.repo")}</label>
          <input
            id="composer-repo"
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            placeholder={t("composer.repo_ph")}
            disabled={submitting}
          />
        </div>
        <div className="field">
          <label htmlFor="composer-provider">{t("composer.provider")}</label>
          <select
            id="composer-provider"
            value={provider}
            onChange={(e) => {
              setProvider(e.target.value);
              setModel("auto");
              setAccount("auto");
            }}
            disabled={submitting}
          >
            <option value="auto">{t("composer.auto")}</option>
            {providers.map((p) => (
              <option key={p.id} value={p.id} disabled={p.needsLogin}>
                {p.label}
                {p.needsLogin ? ` — ${t("integrations.needs_login")}` : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="composer-model">{t("composer.model")}</label>
          <select
            id="composer-model"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            disabled={submitting}
          >
            <option value="auto">{t("composer.auto")}</option>
            {modelOptions.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </div>
      </div>

      <details className="adv-toggle advanced" open={advOpen} data-testid="advanced">
        <summary onClick={(e) => { e.preventDefault(); setAdvOpen(!advOpen); }}>
          {t("composer.advanced")}
        </summary>
        <div className="grid" style={{ marginTop: 10 }}>
          <div className="field">
            <label htmlFor="adv-effort">{t("composer.effort")}</label>
            <select
              id="adv-effort"
              value={effort}
              onChange={(e) => setEffort(e.target.value)}
            >
              <option value="auto">{t("composer.auto")}</option>
              {effortOptions.map((ef) => (
                <option key={ef} value={ef}>{ef}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="adv-account">{t("composer.account")}</label>
            <select
              id="adv-account"
              value={account}
              onChange={(e) => setAccount(e.target.value)}
            >
              <option value="auto">{t("composer.account_auto")}</option>
              {accountOptions.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="adv-delivery">{t("composer.delivery")}</label>
            <select
              id="adv-delivery"
              value={delivery}
              onChange={(e) => setDelivery(e.target.value as DeliveryMode)}
            >
              <option value="none">{t("composer.delivery.none")}</option>
              <option value="branch">{t("composer.delivery.branch")}</option>
              <option value="pr">{t("composer.delivery.pr")}</option>
              <option value="draft_pr">{t("composer.delivery.draft_pr")}</option>
            </select>
          </div>
          {(delivery === "pr" || delivery === "draft_pr") && (
            <div className="field">
              <label htmlFor="adv-delivery-target">{t("composer.delivery_target")}</label>
              <input
                id="adv-delivery-target"
                value={deliveryTarget}
                onChange={(e) => setDeliveryTarget(e.target.value)}
                placeholder={t("composer.delivery_target_ph")}
              />
            </div>
          )}
          {repo.trim() && (
            <div className="field">
              <label htmlFor="adv-repo-ref">{t("composer.repo_ref")}</label>
              <input
                id="adv-repo-ref"
                value={repoRef}
                onChange={(e) => setRepoRef(e.target.value)}
                placeholder={t("composer.repo_ref_ph")}
              />
            </div>
          )}
          <div className="field">
            <label htmlFor="adv-idle">{t("composer.idle_timeout")}</label>
            <input
              id="adv-idle"
              type="number"
              min={1}
              value={idleTimeout}
              onChange={(e) => setIdleTimeout(e.target.value)}
              placeholder="1800"
            />
          </div>
          <div className="field">
            <label htmlFor="adv-cpu">{t("composer.compute_cpu")}</label>
            <input
              id="adv-cpu"
              type="number"
              min={0.5}
              step={0.5}
              value={cpu}
              onChange={(e) => setCpu(e.target.value)}
              placeholder="2"
            />
          </div>
          <div className="field">
            <label htmlFor="adv-mem">{t("composer.compute_mem")}</label>
            <input
              id="adv-mem"
              type="number"
              min={256}
              value={mem}
              onChange={(e) => setMem(e.target.value)}
              placeholder="4096"
            />
          </div>
          <div className="field span2">
            <label htmlFor="adv-secrets">{t("composer.secrets")}</label>
            <input
              id="adv-secrets"
              value={secrets}
              onChange={(e) => setSecrets(e.target.value)}
              placeholder={t("composer.secrets_ph")}
            />
          </div>
          <div className="field span2">
            <label htmlFor="adv-mcp">{t("composer.mcp")}</label>
            <input
              id="adv-mcp"
              value={mcp}
              onChange={(e) => setMcp(e.target.value)}
              placeholder={t("composer.mcp_ph")}
            />
          </div>
        </div>
      </details>

      {validation && (
        <div className="notice warn" role="alert">
          <div className="n-body">{validation}</div>
        </div>
      )}
      <ErrorNotice
        error={error}
        provider={provider === "auto" ? undefined : provider}
        onRetry={() => submit({ preventDefault: () => undefined } as FormEvent)}
        onDismiss={() => setError(null)}
      />

      <div className="send-row">
        <span className="faint small">{providers.length} providers</span>
        <button
          type="submit"
          className="btn btn-primary"
          disabled={submitting}
          data-testid="composer-send"
        >
          {submitting ? <Spinner size={14} /> : <Icon name="send" size={14} />}
          {submitting ? t("composer.sending") : t("composer.send")}
        </button>
      </div>
    </form>
  );
}

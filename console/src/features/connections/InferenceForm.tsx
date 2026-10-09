import { useState, type FormEvent } from "react";
import type { Harness, InferenceConfig, InferenceCredential, InferenceProtocol } from "../../api/types";
import { Field } from "../../components/ui";
import { useI18n } from "../../i18n";
import { harnessesFor, PROTOCOL_NAMES, PROTOCOLS } from "../sessions/harnesses";
import { PRESETS } from "./presets";

type Urls = Partial<Record<InferenceProtocol, string>>;

/**
 * Bring-your-own-key inference entry: an API key, a default model and one base URL per
 * protocol the provider speaks. Only the key is secret; it lives in component state
 * until submit, is cleared synchronously on submit and is never persisted.
 */
export function InferenceForm({
  mode,
  initial,
  initialLabel = "",
  harnesses,
  pending,
  onSubmit,
  onCancel,
}: {
  mode: "create" | "replace";
  /** Current settings when replacing, so rotating a key does not mean retyping URLs. */
  initial?: InferenceConfig;
  initialLabel?: string;
  harnesses?: Harness[];
  pending?: boolean;
  onSubmit: (credential: InferenceCredential, label: string) => void | Promise<void>;
  onCancel?: () => void;
}) {
  const { t } = useI18n();
  const start = mode === "create" ? PRESETS[0] : null;
  const [preset, setPreset] = useState(start?.id ?? "custom");
  const [label, setLabel] = useState(initialLabel || (start?.name ?? ""));
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState(initial?.model ?? start?.model ?? "");
  const [extra, setExtra] = useState((initial?.models ?? []).filter((m) => m !== initial?.model).join(", "));
  const [urls, setUrls] = useState<Urls>({ ...(initial?.endpoints ?? start?.endpoints ?? {}) });
  const id = (n: string) => `${mode}-inference-${n}`;

  const endpoints = Object.fromEntries(
    PROTOCOLS.map((p) => [p, (urls[p] ?? "").trim()] as const).filter(([, url]) => url !== ""),
  ) as Urls;
  const badUrl = PROTOCOLS.find((p) => endpoints[p] && !/^https?:\/\/[^\s/]+/.test(endpoints[p]!));
  const complete = apiKey.trim().length >= 8 && model.trim() !== "" && Object.keys(endpoints).length > 0 && !badUrl;

  const choose = (next: string) => {
    setPreset(next);
    const found = PRESETS.find((p) => p.id === next);
    if (!found) return;
    setUrls({ ...found.endpoints });
    setModel(found.model);
    if (mode === "create") setLabel(found.id === "custom" ? "" : found.name);
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!complete) return;
    const models = extra
      .split(/[\s,]+/)
      .map((m) => m.trim())
      .filter(Boolean);
    const credential: InferenceCredential = {
      api_key: apiKey.trim(),
      model: model.trim(),
      endpoints,
      ...(models.length ? { models } : {}),
    };
    setApiKey(""); // clear before the request is even sent
    void onSubmit(credential, label.trim() || model.trim());
  };

  return (
    <form
      onSubmit={submit}
      autoComplete="off"
      className="inference-form"
      aria-label={t(mode === "create" ? "conn.add_aria" : "conn.replace_aria", { kind: t("kind.inference_api") })}
    >
      <div className="form-grid">
        <Field id={id("preset")} label={t("inference.preset")}>
          <select id={id("preset")} value={preset} onChange={(e) => choose(e.target.value)}>
            {PRESETS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.id === "custom" ? t("inference.preset_custom") : p.name}
              </option>
            ))}
          </select>
        </Field>
        {mode === "create" ? (
          <Field id={id("label")} label={t("conn.label")}>
            <input id={id("label")} value={label} onChange={(e) => setLabel(e.target.value)} maxLength={80} />
          </Field>
        ) : null}
      </div>
      <Field id={id("key")} label={t("inference.api_key")} hint={t("conn.write_only")}>
        <input
          id={id("key")}
          type="password"
          autoComplete="new-password"
          spellCheck={false}
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
      </Field>
      <div className="form-grid">
        <Field id={id("model")} label={t("inference.model")} hint={t("inference.model_hint")}>
          <input
            id={id("model")}
            spellCheck={false}
            placeholder="provider-model-id"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          />
        </Field>
        <Field id={id("models")} label={t("inference.models")} hint={t("inference.models_hint")}>
          <input id={id("models")} spellCheck={false} value={extra} onChange={(e) => setExtra(e.target.value)} />
        </Field>
      </div>
      <fieldset className="protocol-set">
        <legend>{t("inference.protocols")}</legend>
        <p className="faint small">{t("inference.protocols_hint")}</p>
        {PROTOCOLS.map((p) => {
          const users = harnessesFor(harnesses, p);
          return (
            <Field
              key={p}
              id={id(p)}
              label={PROTOCOL_NAMES[p]}
              hint={
                users.length
                  ? t("inference.used_by", { harnesses: users.join(", ") })
                  : undefined
              }
            >
              <input
                id={id(p)}
                type="url"
                inputMode="url"
                spellCheck={false}
                placeholder={p === "anthropic_messages" ? "https://api.example.com/anthropic" : "https://api.example.com/v1"}
                aria-invalid={badUrl === p}
                value={urls[p] ?? ""}
                onChange={(e) => setUrls({ ...urls, [p]: e.target.value })}
              />
            </Field>
          );
        })}
      </fieldset>
      {badUrl ? (
        <p className="hs-note warn" role="alert">
          {t("inference.bad_url")}
        </p>
      ) : null}
      <div className="hs-actions">
        <button type="submit" className="button primary" disabled={!complete || pending}>
          {mode === "create" ? t("conn.connect") : t("conn.replace")}
        </button>
        {onCancel ? (
          <button type="button" className="button ghost" onClick={onCancel}>
            {t("common.cancel")}
          </button>
        ) : null}
      </div>
    </form>
  );
}

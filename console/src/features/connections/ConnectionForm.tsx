import { useState, type FormEvent } from "react";
import type { ConnectionCredential, ConnectionKind } from "../../api/types";
import { Field } from "../../components/ui";
import { useI18n } from "../../i18n";
import { buildCredential, kindMeta } from "./kinds";

/**
 * Write-only credential entry. Secrets live in component state only until submit,
 * are cleared synchronously when the form is submitted, and are never persisted.
 */
export function ConnectionForm({
  kind,
  mode,
  initialLabel = "",
  pending,
  onSubmit,
}: {
  kind: ConnectionKind;
  mode: "create" | "replace";
  initialLabel?: string;
  pending?: boolean;
  onSubmit: (credential: ConnectionCredential, label: string) => void | Promise<void>;
}) {
  const { t } = useI18n();
  const meta = kindMeta(kind)!;
  const [label, setLabel] = useState(initialLabel);
  const [values, setValues] = useState<Record<string, string>>({});
  const id = (n: string) => `${mode}-${kind}-${n}`;
  const complete = meta.fields.every((f) => (values[f.name] ?? "").trim() !== "");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!complete) return;
    const credential = buildCredential(kind, values);
    setValues({}); // clear before the request is even sent
    void onSubmit(credential, label.trim() || kind);
  };

  return (
    <form
      onSubmit={submit}
      autoComplete="off"
      aria-label={t(mode === "create" ? "conn.add_aria" : "conn.replace_aria", { kind: t(meta.title) })}
    >
      {mode === "create" ? (
        <Field id={id("label")} label={t("conn.label")}>
          <input id={id("label")} value={label} onChange={(e) => setLabel(e.target.value)} maxLength={80} />
        </Field>
      ) : null}
      {meta.fields.map((f) => (
        <Field key={f.name} id={id(f.name)} label={t(f.label)}>
          {f.multiline ? (
            <textarea
              id={id(f.name)}
              className="secret-mask"
              rows={4}
              autoComplete="off"
              spellCheck={false}
              value={values[f.name] ?? ""}
              onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
            />
          ) : (
            <input
              id={id(f.name)}
              type={f.secret ? "password" : "text"}
              autoComplete={f.secret ? "new-password" : "off"}
              spellCheck={false}
              value={values[f.name] ?? ""}
              onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
            />
          )}
        </Field>
      ))}
      <p className="faint small">{t("conn.write_only")}</p>
      <button type="submit" className="btn btn-primary" disabled={!complete || pending}>
        {mode === "create" ? t("conn.connect") : t("conn.replace")}
      </button>
    </form>
  );
}

import { useState, type FormEvent } from "react";
import type { ConnectionCredential } from "../../api/types";
import { useI18n } from "../../i18n";
import { Button } from "../../ui/Button";
import { Field, Input, Textarea } from "../../ui/Field";
import { buildCredential, kindMeta, type FieldKind } from "./kinds";

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
  kind: FieldKind;
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
        <Field htmlFor={id("label")} label={t("conn.label")}>
          <Input id={id("label")} value={label} onChange={(e) => setLabel(e.target.value)} maxLength={80} />
        </Field>
      ) : null}
      {meta.fields.map((f) => (
        <Field key={f.name} htmlFor={id(f.name)} label={t(f.label)}>
          {f.multiline ? (
            <Textarea
              id={id(f.name)}
              className="secret-mask"
              rows={4}
              autoComplete="off"
              spellCheck={false}
              value={values[f.name] ?? ""}
              onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}
            />
          ) : (
            <Input
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
      <Button type="submit" variant="primary" disabled={!complete || pending}>
        {mode === "create" ? t("conn.connect") : t("conn.replace")}
      </Button>
    </form>
  );
}

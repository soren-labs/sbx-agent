import type { ConnectionCredential, ConnectionKind } from "../../api/types";
import type { I18nKey } from "../../i18n/en";

export interface SecretField {
  name: string;
  label: I18nKey;
  /** Multi-line secrets use a masked textarea; the rest are password inputs. */
  multiline?: boolean;
  secret: boolean;
}

/** Kinds entered through the generic secret-field form; inference has its own form. */
export type FieldKind = Exclude<ConnectionKind, "inference_api">;

export interface KindMeta {
  kind: FieldKind;
  title: I18nKey;
  purpose: I18nKey;
  required: boolean;
  fields: SecretField[];
}

export const KINDS: KindMeta[] = [
  {
    kind: "modal",
    title: "kind.modal",
    purpose: "kind.modal.purpose",
    required: true,
    fields: [
      { name: "token_id", label: "field.modal_token_id", secret: false },
      { name: "token_secret", label: "field.modal_token_secret", secret: true },
    ],
  },
  {
    kind: "github",
    title: "kind.github",
    purpose: "kind.github.purpose",
    required: true,
    fields: [{ name: "token", label: "field.github_token", secret: true }],
  },
];

export const kindMeta = (kind: string) => KINDS.find((k) => k.kind === kind);

export function buildCredential(kind: FieldKind, values: Record<string, string>): ConnectionCredential {
  const out: Record<string, string> = {};
  for (const f of kindMeta(kind)?.fields ?? []) out[f.name] = (values[f.name] ?? "").trim();
  return out as unknown as ConnectionCredential;
}

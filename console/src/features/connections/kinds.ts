import type { ConnectionCredential, ConnectionKind } from "../../api/types";
import type { I18nKey } from "../../i18n/en";

export interface SecretField {
  name: string;
  label: I18nKey;
  /** Multi-line secrets (auth.json) use a masked textarea; the rest are password inputs. */
  multiline?: boolean;
  secret: boolean;
}

export interface KindMeta {
  kind: ConnectionKind;
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
  {
    kind: "opencode_zen",
    title: "kind.opencode_zen",
    purpose: "kind.opencode_zen.purpose",
    required: true,
    fields: [{ name: "api_key", label: "field.zen_key", secret: true }],
  },
  {
    kind: "codex",
    title: "kind.codex",
    purpose: "kind.codex.purpose",
    required: false,
    fields: [{ name: "auth_json", label: "field.codex_auth", secret: true, multiline: true }],
  },
];

export const kindMeta = (kind: string) => KINDS.find((k) => k.kind === kind);

export function buildCredential(kind: ConnectionKind, values: Record<string, string>): ConnectionCredential {
  const out: Record<string, string> = {};
  for (const f of kindMeta(kind)?.fields ?? []) out[f.name] = (values[f.name] ?? "").trim();
  return out as unknown as ConnectionCredential;
}

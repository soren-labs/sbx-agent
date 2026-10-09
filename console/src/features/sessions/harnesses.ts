import type { Harness, InferenceProtocol } from "../../api/types";

/** Product names of the official CLIs; unknown providers fall back to their id. */
const NAMES: Record<string, string> = {
  opencode: "OpenCode",
  codex: "Codex",
  claude: "Claude Code",
  grok: "Grok Build",
  commandcode: "Command Code",
};

export const PROTOCOLS: InferenceProtocol[] = ["openai_chat", "openai_responses", "anthropic_messages"];

export const PROTOCOL_NAMES: Record<InferenceProtocol, string> = {
  openai_chat: "OpenAI Chat Completions",
  openai_responses: "OpenAI Responses",
  anthropic_messages: "Anthropic Messages",
};

export const harnessName = (id: string) => NAMES[id] ?? id;

/** Harnesses a Session can actually run on (disabled ones are never offered). */
export const selectable = (items: Harness[] | undefined) =>
  (items ?? []).filter((h) => h.support_tier !== "disabled");

/** Names of the selectable Harnesses that can use an endpoint of this protocol. */
export const harnessesFor = (items: Harness[] | undefined, protocol: InferenceProtocol) =>
  selectable(items)
    .filter((h) => (h.inference_protocols ?? []).includes(protocol))
    .map((h) => harnessName(h.provider_id));

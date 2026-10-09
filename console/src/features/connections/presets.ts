import type { InferenceProtocol } from "../../api/types";

export interface InferencePreset {
  id: string;
  name: string;
  model: string;
  endpoints: Partial<Record<InferenceProtocol, string>>;
}

/**
 * Starting points only: every field stays editable and any provider that speaks one of
 * the protocols works through "Custom".
 */
export const PRESETS: InferencePreset[] = [
  {
    id: "deepseek",
    name: "DeepSeek",
    model: "deepseek-flash",
    endpoints: {
      openai_chat: "https://api.deepseek.com",
      openai_responses: "https://api.deepseek.com",
      anthropic_messages: "https://api.deepseek.com/anthropic",
    },
  },
  { id: "custom", name: "Custom", model: "", endpoints: {} },
];

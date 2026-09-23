/** Product vocabulary: statuses, providers and error explanations. */
import { t } from "./i18n.js";

export const PROVIDERS = ["codex", "devin", "antigravity", "grok", "opencode"];

export const PROVIDER_META = {
  codex: {
    label: "Codex",
    tier: "stable",
    credential: ".codex/auth.json",
    login: "codex login",
  },
  devin: {
    label: "Devin",
    tier: "experimental",
    credential: ".local/share/devin/credentials.toml",
    login: "devin",
  },
  antigravity: {
    label: "Antigravity",
    tier: "experimental",
    credential: ".gemini/antigravity-cli/antigravity-oauth-token",
    login: "agy",
  },
  grok: { label: "Grok", tier: "experimental", credential: ".grok/auth.json", login: "grok" },
  opencode: {
    label: "OpenCode",
    tier: "experimental",
    credential: ".local/share/opencode/auth.json",
    login: "opencode auth login",
  },
};

export function providerLabel(provider) {
  return PROVIDER_META[provider]?.label || provider || "—";
}

const AGENT_STATUS = {
  creating: { tone: "violet", label: "Starting", live: true },
  idle: { tone: "teal", label: "Idle" },
  running: { tone: "blue", label: "Running", live: true },
  closed: { tone: "neutral", label: "Closed", ended: true },
  timed_out: { tone: "amber", label: "Timed out", ended: true },
  lost: { tone: "red", label: "Lost", ended: true },
  missing: { tone: "neutral", label: "Missing", ended: true },
};

const RUN_STATUS = {
  CREATING: { tone: "violet", label: "Starting", live: true },
  RUNNING: { tone: "blue", label: "Running", live: true },
  FINISHED: { tone: "green", label: "Finished", ended: true },
  ERROR: { tone: "red", label: "Error", ended: true },
  CANCELLED: { tone: "neutral", label: "Cancelled", ended: true },
  EXPIRED: { tone: "amber", label: "Expired", ended: true },
  UNKNOWN: { tone: "neutral", label: "Unknown", ended: true },
};

const ACCOUNT_STATUS = {
  active: { tone: "green", label: "Active" },
  cooling: { tone: "amber", label: "Cooling down" },
  invalid: { tone: "red", label: "Invalid" },
  disabled: { tone: "neutral", label: "Disabled" },
};

const TABLES = { agent: AGENT_STATUS, run: RUN_STATUS, account: ACCOUNT_STATUS };

export function statusMeta(kind, status) {
  const meta = TABLES[kind]?.[status];
  return meta
    ? { ...meta, label: t(meta.label) }
    : { tone: "neutral", label: status || t("Unknown") };
}

export const isAgentLive = (status) => ["creating", "idle", "running"].includes(status);
export const isAgentEnded = (status) => Boolean(AGENT_STATUS[status]?.ended);
export const isRunLive = (status) => status === "CREATING" || status === "RUNNING";

/** API error codes (`{error:{code}}`) → what it means for the operator. */
const API_ERROR_HELP = {
  unauthorized: "The API key is missing, wrong, or revoked.",
  forbidden: "This action needs an API key with the admin scope.",
  not_found: "It does not exist (or belongs to another key).",
  invalid_provider: "The request was rejected as malformed or names an unknown provider.",
  invalid_request: "The request was rejected as malformed.",
  turn_in_progress: "A run is still in progress. Wait for it to finish or cancel it first.",
  session_not_runnable: "This agent is closed and can no longer take runs.",
  account_busy: "The chosen account has no free slot right now.",
  account_unavailable: "The chosen account is not active (cooling down, invalid or disabled).",
  provider_exhausted: "No account for this provider has a free slot. Try again shortly.",
  concurrency_limit: "The live-agent limit for this key is reached. Close idle agents first.",
  idempotency_conflict: "The idempotency key was already used with a different request.",
  idempotency_in_progress: "A request with the same idempotency key is still being processed.",
  workspace_invalid: "The workspace or git policy declaration is invalid.",
  workspace_not_found: "This agent has no repository workspace.",
  repo_unavailable: "The repository could not be cloned, fetched or pushed.",
  checkout_failed: "The base ref or sha does not resolve in the repository.",
  base_sha_mismatch: "The base ref resolved to a different commit than the declared base sha.",
  head_sha_mismatch: "The head sha disagrees with the recorded workspace head.",
  checksum_mismatch: "The artifact failed its integrity check.",
  artifact_not_found: "The artifact does not exist.",
  artifact_invalid: "The artifact is malformed or corrupt.",
  artifact_secret: "Credential-like content was found in the workspace; nothing was stored.",
  review_required: "Merging needs an exact-sha review pin on the current head first.",
  invalid_output_contract: "The output contract schema uses unsupported JSON Schema features.",
  invalid_resource: "A secret or MCP reference is unknown or not allowed.",
  invalid_compute: "The compute request is malformed or out of bounds.",
  unsupported: "The provider does not support this option.",
  github_app_unconfigured: "No GitHub App is configured on the control plane.",
  github_app_invalid: "The authorization callback was malformed.",
  github_app_state: "The authorization state expired or was already used. Start again.",
  github_app_upstream: "A GitHub API call failed. Try again later.",
  network_error: "The control plane could not be reached.",
};

export function explainApiError(err) {
  const code = err?.code || "error";
  const help = API_ERROR_HELP[code];
  return { code, title: help ? t(help) : err?.message || t("Request failed"), detail: err?.message };
}

/** Run error codes (`run.error.code`) → explanation + next step. */
const RUN_ERROR_HELP = {
  auth_invalid: [
    "The provider rejected the account credential.",
    "Log in again with the provider CLI and re-import the account, then verify it.",
  ],
  rate_limited: [
    "The provider is rate limiting this account.",
    "Retry later; the scheduler cools the account down and prefers others.",
  ],
  quota_exhausted: [
    "The subscription quota for this account is used up.",
    "Add another account or wait for the quota to reset.",
  ],
  model_unavailable: [
    "The requested model is not available to this account.",
    "Pick a model listed on the Capacity page.",
  ],
  model_capacity: ["The provider is out of capacity for this model.", "Retry later or use another model."],
  provider_unavailable: ["The provider service is unavailable.", "Retry later."],
  runtime_error: [
    "The sandbox or runner failed.",
    "Check the activity log; retrying on a fresh agent usually helps.",
  ],
  event_parse_error: [
    "The provider's event stream could not be parsed, so success cannot be confirmed.",
    "Treat the result as untrusted; retry the run.",
  ],
  timeout: ["The run hit the per-turn time limit.", "Split the task or raise the turn limit."],
  cancelled: ["The run was cancelled.", "Send a new message to continue."],
  contract_violation: [
    "The final message did not satisfy the output contract.",
    "See the violations below and adjust the prompt or schema.",
  ],
};

export function explainRunError(error) {
  const help = RUN_ERROR_HELP[error?.code];
  return help ? { what: t(help[0]), next: t(help[1]) } : { what: error?.message || "", next: "" };
}

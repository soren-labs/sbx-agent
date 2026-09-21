// Mock data for the SOR-171 Control Center prototype.
// Field names mirror docs/contracts/api-v1.yaml exactly.

export type ProviderId = "codex" | "antigravity" | "grok" | "opencode" | "devin";
export type AgentStatus = "creating" | "idle" | "running" | "closed" | "timed_out" | "lost";
export type RunStatus = "CREATING" | "RUNNING" | "FINISHED" | "ERROR" | "CANCELLED" | "EXPIRED" | "UNKNOWN";
export type AccountStatus = "active" | "cooling" | "invalid" | "disabled";

export interface Usage {
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  cache_write_input_tokens?: number;
  reasoning_output_tokens?: number;
}

export interface RunError {
  code: string; // run_error_codes
  source: "provider" | "runtime" | "control" | "telemetry";
  message: string;
  retryable: boolean;
  retry_after?: number;
}

export interface OutputContractResult {
  schema?: object | null;
  enforcement: "strict" | "warn";
  schema_digest?: string | null;
  status: "pending" | "valid" | "invalid" | "skipped";
  extraction?: "raw" | "fence" | "embedded" | null;
  violations: { path: string; code: string; message: string }[];
}

export interface Run {
  id: string;
  agent_id: string;
  status: RunStatus;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  result?: { text: string } | null;
  error?: RunError | null;
  usage?: Usage;
  provider?: ProviderId | null;
  account_id?: string | null;
  model?: string | null;
  artifact_refs?: string[];
  structured_output?: unknown;
  output_contract?: OutputContractResult | null;
}

export interface WorkflowMetadata {
  workflow_id: string;
  task_id: string;
  role: string;
  parent_task_id?: string | null;
}

export interface SessionResources {
  secrets?: string[];
  mcp?: string[];
}

export interface Agent {
  id: string;
  name: string;
  provider: ProviderId;
  account_id: string;
  model: string;
  status: AgentStatus;
  created_at: string;
  updated_at: string;
  usage?: Usage | null;
  cost_estimate_usd?: number;
  metadata?: WorkflowMetadata | null;
  resources?: SessionResources | null;
}

export interface WorkspaceRecord {
  agent_id: string;
  repo: string;
  base_ref: string;
  base_sha: string;
  workdir?: string;
  checkout_sha?: string | null;
  head_sha?: string | null;
  reviewed_head_sha?: string | null;
  git?: {
    branch?: string;
    push?: boolean;
    auto_create_pr?: boolean;
    target?: string;
    draft?: boolean;
    title?: string;
  } | null;
  branch?: string | null;
  pushed_head_sha?: string | null;
  pull_request?: {
    number: number;
    url: string;
    state: string;
    ref: string;
    head_sha: string;
    base: string;
    draft: boolean;
    review_comment_url?: string;
  } | null;
  created_at: string;
  updated_at: string;
}

export interface Artifact {
  schema_version: number;
  artifact_id: string;
  format: "patch";
  base_sha: string;
  head_sha: string;
  repo: string;
  created_at: string;
  producer: { agent_id: string; run_id?: string | null };
  files: { path: string; sha256: string; size: number }[];
  tests: { command: string; exit_code: number }[];
  payloads: Record<string, string>;
  warnings: string[];
  download_url: string;
}

export interface WorkflowAgent {
  agent_id: string;
  task_id: string;
  role: string;
  parent_task_id?: string | null;
  attached_at: string;
  status: AgentStatus | "missing";
  provider?: ProviderId | null;
  account_id?: string | null;
  model?: string | null;
  agent_created_at?: string | null;
  runs: number;
  latest_run?: Run | null;
}

export interface Workflow {
  workflow_id: string;
  agents: WorkflowAgent[];
  progress: {
    tasks: number;
    agents: number;
    open_agents: number;
    runs: number;
    latest_runs_by_status: Record<string, number>;
    all_terminal: boolean;
  };
}

export interface Account {
  id: string;
  provider: ProviderId;
  label: string;
  status: AccountStatus;
  max_concurrent: number;
  running: number;
  models: string[];
  created_at: string;
  last_used_at?: string | null;
  cooldown_until?: string | null;
  last_error?: string | null;
}

export interface ApiKey {
  id: string;
  label: string;
  scopes: ("agents" | "admin")[];
  created_at: string;
  revoked_at?: string | null;
}

export interface Model {
  provider: ProviderId;
  model: string;
  accounts_available: number;
}

export interface Me {
  key_id: string;
  label: string;
  scopes: ("agents" | "admin")[];
}

export interface ErrorBody {
  error: { code: string; message: string; retry_after?: number };
}

// Canonical event types for the run timeline (events.md / codex_events + item_types)
export interface TimelineEvent {
  id: number; // events.jsonl line number
  type: string; // turn.started, item.completed, ...
  item_type?: string; // agent_message | command_execution | file_change | reasoning | error
  title: string;
  detail?: string;
  at: string;
  exit_code?: number;
}

// ---------------------------------------------------------------------------

export const PROVIDERS: {
  id: ProviderId;
  status: "Stable" | "Experimental" | "Preview" | "Not supported";
  cli: string;
  authFile: string;
  multiTurn: string;
  cancel: boolean;
  multiAccount: string;
  evidence: string;
}[] = [
  {
    id: "codex",
    status: "Stable",
    cli: "@openai/codex 0.153.0 (image-pinned)",
    authFile: "~/.codex/auth.json",
    multiTurn: "codex exec resume",
    cancel: true,
    multiAccount: "SBX_CODEX_ACCOUNTS",
    evidence: "Real-Modal suite + committed timings.json; RC gate lane CREDENTIAL_DEFERRED (stale ChatGPT token)",
  },
  {
    id: "devin",
    status: "Experimental",
    cli: "Devin CLI 3000.10.21 (sha256-pinned)",
    authFile: "~/.local/share/devin/credentials.toml",
    multiTurn: "ACP session",
    cancel: true,
    multiAccount: "SBX_DEVIN_ACCOUNTS / burst slots",
    evidence: "Release 0.1 /v1 real-Modal gate PASS; 8-way concurrency PASS (2026-09-14)",
  },
  {
    id: "antigravity",
    status: "Experimental",
    cli: "your agy binary (agy_version 1.2.3)",
    authFile: "~/.gemini/antigravity-cli/antigravity-oauth-token",
    multiTurn: "--conversation <id>",
    cancel: true,
    multiAccount: "SBX_ANTIGRAVITY_ACCOUNTS",
    evidence: "SOR-68 fleet gate PASS 50/50 on RC plane",
  },
  {
    id: "grok",
    status: "Experimental",
    cli: "your grok binary (verified 1.0.24)",
    authFile: "~/.grok/auth.json",
    multiTurn: "--resume <id>",
    cancel: true,
    multiAccount: "SBX_GROK_ACCOUNTS",
    evidence: "SOR-68 runner + fleet gates PASS on RC plane",
  },
  {
    id: "opencode",
    status: "Experimental",
    cli: "opencode-ai 1.18.29 (npm-pinned)",
    authFile: "~/.local/share/opencode/auth.json",
    multiTurn: "--session <id>",
    cancel: true,
    multiAccount: "SBX_OPENCODE_ACCOUNTS",
    evidence: "Release 0.1 real-account gate PASS on RC plane",
  },
];

const now = Date.now();
const iso = (minAgo: number) => new Date(now - minAgo * 60_000).toISOString();
const sha = (s: string) => s.padEnd(40, "0").slice(0, 40);

export const AGENTS: Agent[] = [
  {
    id: "ag_7f3k2",
    name: "Fix flaky date test",
    provider: "codex",
    account_id: "codex-1",
    model: "gpt-5.2-codex",
    status: "running",
    created_at: iso(42),
    updated_at: iso(1),
    usage: { input_tokens: 48_210, cached_input_tokens: 31_400, output_tokens: 6_120 },
    cost_estimate_usd: 0.37,
    metadata: { workflow_id: "wf-release-012", task_id: "fix-flaky-date", role: "worker" },
  },
  {
    id: "ag_9x1mq",
    name: "Reviewer: pinned PR #42 head",
    provider: "devin",
    account_id: "devin-1",
    model: "devin-default",
    status: "idle",
    created_at: iso(88),
    updated_at: iso(12),
    usage: { input_tokens: 92_050, cached_input_tokens: 60_300, output_tokens: 14_880, reasoning_output_tokens: 4_120 },
    cost_estimate_usd: 0.81,
    metadata: { workflow_id: "wf-release-012", task_id: "review-wp-h1", role: "reviewer", parent_task_id: "oauth-writeback" },
    resources: { mcp: ["github-bridge"] },
  },
  {
    id: "ag_2p8wr",
    name: "Migrate web/ to /v1 reads",
    provider: "opencode",
    account_id: "opencode-1",
    model: "openai/gpt-5.6-luna",
    status: "idle",
    created_at: iso(300),
    updated_at: iso(95),
    usage: { input_tokens: 121_400, cached_input_tokens: 88_100, output_tokens: 21_040 },
    cost_estimate_usd: 1.12,
  },
  {
    id: "ag_4t6yv",
    name: "Nightly soak: 20 turn sequence",
    provider: "grok",
    account_id: "grok-2",
    model: "grok-code-fast-1",
    status: "timed_out",
    created_at: iso(1_500),
    updated_at: iso(1_140),
    usage: { input_tokens: 402_700, cached_input_tokens: 210_500, output_tokens: 55_300 },
    cost_estimate_usd: 2.94,
  },
  {
    id: "ag_8b3ns",
    name: "Import docs sweep",
    provider: "antigravity",
    account_id: "agy-1",
    model: "gemini-3-pro",
    status: "closed",
    created_at: iso(2_300),
    updated_at: iso(2_010),
    usage: { input_tokens: 33_900, cached_input_tokens: 12_400, output_tokens: 4_010 },
    cost_estimate_usd: 0.22,
  },
  {
    id: "ag_5m9kc",
    name: "Contract lint for api-v1",
    provider: "codex",
    account_id: "codex-2",
    model: "gpt-5.2-codex",
    status: "lost",
    created_at: iso(4_300),
    updated_at: iso(4_100),
    usage: null,
    cost_estimate_usd: 0,
  },
];

export const RUNS: Record<string, Run[]> = {
  ag_7f3k2: [
    {
      id: "run_01",
      agent_id: "ag_7f3k2",
      status: "FINISHED",
      provider: "codex",
      account_id: "codex-1",
      model: "gpt-5.2-codex",
      created_at: iso(42),
      updated_at: iso(31),
      started_at: iso(42),
      finished_at: iso(31),
      result: { text: "Reproduced the flake (timezone-sensitive assertion), pinned TZ=UTC in the fixture." },
      usage: { input_tokens: 30_100, cached_input_tokens: 19_200, output_tokens: 3_900 },
      artifact_refs: ["inbox/1.md", "turns/1.json", "events.jsonl", "events.raw.jsonl"],
      output_contract: { enforcement: "strict", status: "valid", extraction: "fence", schema_digest: "sha256:9c2b…4e1a", violations: [] },
      structured_output: { fixed: true, files_changed: 2, tests_passed: true },
    },
    {
      id: "run_02",
      agent_id: "ag_7f3k2",
      status: "RUNNING",
      provider: "codex",
      account_id: "codex-1",
      model: "gpt-5.2-codex",
      created_at: iso(6),
      updated_at: iso(0),
      started_at: iso(6),
      finished_at: null,
      artifact_refs: ["inbox/2.md", "events.jsonl", "events.raw.jsonl"],
      output_contract: { enforcement: "strict", status: "pending", violations: [] },
    },
  ],
  ag_9x1mq: [
    {
      id: "run_01",
      agent_id: "ag_9x1mq",
      status: "ERROR",
      provider: "devin",
      account_id: "devin-1",
      model: "devin-default",
      created_at: iso(80),
      updated_at: iso(71),
      started_at: iso(80),
      finished_at: iso(71),
      error: {
        code: "head_sha_mismatch",
        source: "control",
        message: "pull_request ref refs/pull/42/head resolved to 3f9a… but pinned head_sha was b178…",
        retryable: true,
      },
      artifact_refs: ["turns/1.json", "events.jsonl"],
    },
    {
      id: "run_02",
      agent_id: "ag_9x1mq",
      status: "FINISHED",
      provider: "devin",
      account_id: "devin-1",
      model: "devin-default",
      created_at: iso(60),
      updated_at: iso(12),
      started_at: iso(60),
      finished_at: iso(12),
      result: { text: "Reviewed head b178394… — credential write-back verified, no token material in events." },
      usage: { input_tokens: 61_950, cached_input_tokens: 41_100, output_tokens: 10_980, reasoning_output_tokens: 4_120 },
      artifact_refs: ["turns/2.json", "events.jsonl", "artifact://art_3q9z"],
      structured_output: { verdict: "approve-with-comments", findings: 2, blocking: 0 },
      output_contract: { enforcement: "warn", status: "valid", extraction: "raw", schema_digest: "sha256:71af…0d22", violations: [] },
    },
  ],
  ag_4t6yv: [
    {
      id: "run_11",
      agent_id: "ag_4t6yv",
      status: "ERROR",
      provider: "grok",
      account_id: "grok-2",
      model: "grok-code-fast-1",
      created_at: iso(1_200),
      updated_at: iso(1_140),
      started_at: iso(1_200),
      finished_at: iso(1_140),
      error: {
        code: "rate_limited",
        source: "provider",
        message: "Provider returned 429; account grok-2 entered cooldown",
        retryable: true,
        retry_after: 300,
      },
      usage: { input_tokens: 88_300, cached_input_tokens: 40_000, output_tokens: 11_200 },
      artifact_refs: ["turns/11.json", "events.jsonl"],
    },
  ],
};

export const TIMELINE: Record<string, TimelineEvent[]> = {
  "ag_7f3k2/run_02": [
    { id: 148, type: "sbx.turn_started", title: "Run 2 started — follow-up prompt", at: iso(6) },
    { id: 149, type: "turn.started", title: "turn.started", at: iso(6) },
    { id: 150, type: "item.completed", item_type: "reasoning", title: "Reasoning", detail: "The earlier fix pinned TZ at the fixture level; re-checking the parser boundary where wall-clock leaks in…", at: iso(5) },
    { id: 151, type: "item.completed", item_type: "command_execution", title: "$ pytest tests/unit/test_dates.py -x", detail: "2 failed, 41 passed — flake reproduces under TZ=America/Los_Angeles", exit_code: 1, at: iso(4) },
    { id: 152, type: "item.completed", item_type: "file_change", title: "control/dates.py", detail: "+12 −4 — use injected clock in parse_window()", at: iso(3) },
    { id: 153, type: "item.started", item_type: "command_execution", title: "$ pytest tests/unit/test_dates.py", detail: "streaming…", at: iso(1) },
  ],
  "ag_9x1mq/run_02": [
    { id: 88, type: "turn.started", title: "turn.started", at: iso(60) },
    { id: 89, type: "item.completed", item_type: "reasoning", title: "Reasoning", detail: "Pinned head b178394…; checking write-back ordering vs reaper window…", at: iso(55) },
    { id: 90, type: "item.completed", item_type: "command_execution", title: "$ git log --oneline -3", exit_code: 0, detail: "b178394 SOR-147 OAuth credential auto-refresh write-back", at: iso(50) },
    { id: 91, type: "item.completed", item_type: "agent_message", title: "Agent message", detail: "Reviewed head b178394… — credential write-back verified, no token material in events.", at: iso(12) },
    { id: 92, type: "turn.completed", title: "turn.completed — FINISHED", at: iso(12) },
  ],
};

export const WORKSPACES: Record<string, WorkspaceRecord> = {
  ag_7f3k2: {
    agent_id: "ag_7f3k2",
    repo: "https://github.com/soren-labs/sbx-browser",
    base_ref: "main",
    base_sha: sha("b1783949432b982291c71c6efee7afea184a638e"),
    workdir: "repo",
    checkout_sha: sha("b1783949432b982291c71c6efee7afea184a638e"),
    head_sha: sha("c4aa01f93bb7710e2ad5fe19d03b19e67a20a1c9"),
    reviewed_head_sha: null,
    git: { branch: "sbx/flaky-date", push: true, auto_create_pr: true, target: "main", draft: true, title: "Fix flaky date test" },
    branch: "sbx/flaky-date",
    pushed_head_sha: null,
    pull_request: null,
    created_at: iso(42),
    updated_at: iso(1),
  },
  ag_9x1mq: {
    agent_id: "ag_9x1mq",
    repo: "https://github.com/soren-labs/sbx-browser",
    base_ref: "main",
    base_sha: sha("b1783949432b982291c71c6efee7afea184a638e"),
    workdir: "repo",
    checkout_sha: sha("3f9a47c2e0d8116c5b0f4a32d9981f0aabb44e21"),
    head_sha: sha("3f9a47c2e0d8116c5b0f4a32d9981f0aabb44e21"),
    reviewed_head_sha: sha("3f9a47c2e0d8116c5b0f4a32d9981f0aabb44e21"),
    git: null,
    branch: null,
    pushed_head_sha: sha("3f9a47c2e0d8116c5b0f4a32d9981f0aabb44e21"),
    pull_request: {
      number: 42,
      url: "https://github.com/soren-labs/sbx-browser/pull/42",
      state: "open",
      ref: "refs/pull/42/head",
      head_sha: sha("3f9a47c2e0d8116c5b0f4a32d9981f0aabb44e21"),
      base: "main",
      draft: false,
      review_comment_url: "https://api.github.com/repos/soren-labs/sbx-browser/issues/42/comments",
    },
    created_at: iso(88),
    updated_at: iso(12),
  },
};

export const ARTIFACTS: Artifact[] = [
  {
    schema_version: 1,
    artifact_id: "art_3q9z",
    format: "patch",
    base_sha: sha("b1783949432b982291c71c6efee7afea184a638e"),
    head_sha: sha("3f9a47c2e0d8116c5b0f4a32d9981f0aabb44e21"),
    repo: "https://github.com/soren-labs/sbx-browser",
    created_at: iso(15),
    producer: { agent_id: "ag_9x1mq", run_id: "run_02" },
    files: [
      { path: "control/onboarding.py", sha256: "9c2b4e1a…", size: 18_420 },
      { path: "tests/unit/control/test_writeback.py", sha256: "71af0d22…", size: 6_130 },
    ],
    tests: [{ command: "pytest tests/unit/control -q", exit_code: 0 }],
    payloads: { "patch.diff": "ab0198…", "repo.bundle": "f47c21…", "manifest.json": "02dd9a…" },
    warnings: [],
    download_url: "/v1/artifacts/art_3q9z/download",
  },
  {
    schema_version: 1,
    artifact_id: "art_8k2m",
    format: "patch",
    base_sha: sha("a3794611111111111111111111111111111111"),
    head_sha: sha("aa1002f00bb7710e2ad5fe19d03b19e67a20a1c9"),
    repo: "https://github.com/soren-labs/sbx-browser",
    created_at: iso(1_100),
    producer: { agent_id: "ag_4t6yv", run_id: "run_11" },
    files: [{ path: "web/app.js", sha256: "5e10cc…", size: 41_002 }],
    tests: [{ command: "make lint", exit_code: 0 }],
    payloads: { "patch.diff": "77d2e0…", "manifest.json": "19bc33…" },
    warnings: ["repo.bundle omitted: worktree dirty at snapshot time"],
    download_url: "/v1/artifacts/art_8k2m/download",
  },
];

export const WORKFLOWS: Workflow[] = [
  {
    workflow_id: "wf-release-012",
    agents: [
      {
        agent_id: "ag_7f3k2",
        task_id: "fix-flaky-date",
        role: "worker",
        attached_at: iso(42),
        status: "running",
        provider: "codex",
        account_id: "codex-1",
        model: "gpt-5.2-codex",
        agent_created_at: iso(42),
        runs: 2,
        latest_run: RUNS["ag_7f3k2"][1],
      },
      {
        agent_id: "ag_9x1mq",
        task_id: "review-wp-h1",
        role: "reviewer",
        parent_task_id: "oauth-writeback",
        attached_at: iso(88),
        status: "idle",
        provider: "devin",
        account_id: "devin-1",
        model: "devin-default",
        agent_created_at: iso(88),
        runs: 2,
        latest_run: RUNS["ag_9x1mq"][1],
      },
      {
        agent_id: "ag_dead0",
        task_id: "soak-20t",
        role: "worker",
        attached_at: iso(1_400),
        status: "missing",
        provider: "grok",
        account_id: null,
        model: null,
        agent_created_at: null,
        runs: 0,
        latest_run: null,
      },
    ],
    progress: {
      tasks: 3,
      agents: 3,
      open_agents: 2,
      runs: 4,
      latest_runs_by_status: { RUNNING: 1, FINISHED: 1 },
      all_terminal: false,
    },
  },
];

export const ACCOUNTS: Account[] = [
  { id: "codex-1", provider: "codex", label: "ChatGPT (team)", status: "active", max_concurrent: 2, running: 1, models: ["gpt-5.2-codex", "gpt-5.1-codex-mini"], created_at: iso(20_000), last_used_at: iso(1) },
  { id: "codex-2", provider: "codex", label: "ChatGPT (backup)", status: "invalid", max_concurrent: 1, running: 0, models: ["gpt-5.2-codex"], created_at: iso(30_000), last_used_at: iso(4_100), last_error: "auth_invalid" },
  { id: "devin-1", provider: "devin", label: "Devin org seat", status: "active", max_concurrent: 8, running: 0, models: ["devin-default"], created_at: iso(15_000), last_used_at: iso(12) },
  { id: "agy-1", provider: "antigravity", label: "agy oauth", status: "cooling", max_concurrent: 4, running: 0, models: ["gemini-3-pro"], created_at: iso(10_000), last_used_at: iso(2_010), cooldown_until: iso(-4), last_error: "rate_limited" },
  { id: "grok-1", provider: "grok", label: "grok work", status: "active", max_concurrent: 2, running: 0, models: ["grok-code-fast-1"], created_at: iso(9_000), last_used_at: iso(3_000) },
  { id: "grok-2", provider: "grok", label: "grok soak", status: "cooling", max_concurrent: 2, running: 0, models: ["grok-code-fast-1"], created_at: iso(9_000), last_used_at: iso(1_140), cooldown_until: iso(-2), last_error: "rate_limited" },
  { id: "opencode-1", provider: "opencode", label: "OpenAI OAuth channel", status: "active", max_concurrent: 2, running: 0, models: ["openai/gpt-5.6-luna"], created_at: iso(8_000), last_used_at: iso(95) },
];

export const MODELS: Model[] = [
  { provider: "codex", model: "gpt-5.2-codex", accounts_available: 1 },
  { provider: "codex", model: "gpt-5.1-codex-mini", accounts_available: 1 },
  { provider: "devin", model: "devin-default", accounts_available: 1 },
  { provider: "antigravity", model: "gemini-3-pro", accounts_available: 0 },
  { provider: "grok", model: "grok-code-fast-1", accounts_available: 1 },
  { provider: "opencode", model: "openai/gpt-5.6-luna", accounts_available: 1 },
];

export const API_KEYS: ApiKey[] = [
  { id: "key_01", label: "bootstrap (admin)", scopes: ["agents", "admin"], created_at: iso(40_000) },
  { id: "key_02", label: "ci-orchestrator", scopes: ["agents"], created_at: iso(12_000) },
  { id: "key_03", label: "old-local-test", scopes: ["agents"], created_at: iso(50_000), revoked_at: iso(20_000) },
];

export const ME: Me = { key_id: "key_01", label: "bootstrap (admin)", scopes: ["agents", "admin"] };

export type Scenario = "live" | "empty" | "degraded" | "coldstart";

export const SCENARIOS: { id: Scenario; label: string; hint: string }[] = [
  { id: "live", label: "Healthy fleet", hint: "Agents running, accounts verified" },
  { id: "coldstart", label: "Fresh deploy", hint: "No agents yet, no accounts imported" },
  { id: "empty", label: "All idle", hint: "Fleet exists, nothing running" },
  { id: "degraded", label: "Degraded", hint: "Credentials unready, rate limits, lost agents" },
];

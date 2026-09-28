import type {
  IntegrationStatus,
  ProviderInfo,
  Session,
  SessionChange,
  Turn,
} from "./types";

/**
 * Typed local fixtures for the V2 console. They exist so UI work proceeds
 * while SOR-256 lands; every value matches the contract shapes (never real
 * credentials — tokens are REDACTED-style placeholders only).
 */

const now = Date.now();
const iso = (msAgo: number) => new Date(now - msAgo).toISOString();

export const PROVIDERS: ProviderInfo[] = [
  {
    id: "codex",
    label: "Codex",
    models: ["gpt-5-codex", "gpt-5", "o4-mini"],
    efforts: ["none", "minimal", "low", "medium", "high"],
    accountsTotal: 2,
    accountsAvailable: 1,
    needsLogin: false,
  },
  {
    id: "antigravity",
    label: "Antigravity",
    models: ["agy-pro", "agy-standard"],
    efforts: ["low", "medium", "high", "xhigh"],
    accountsTotal: 1,
    accountsAvailable: 0,
    needsLogin: false,
  },
  {
    id: "grok",
    label: "Grok",
    models: ["grok-code-fast", "grok-4"],
    efforts: ["low", "medium", "high"],
    accountsTotal: 1,
    accountsAvailable: 1,
    needsLogin: false,
  },
  {
    id: "opencode",
    label: "OpenCode",
    models: ["oc-sonnet", "oc-haiku"],
    efforts: ["minimal", "low", "medium", "high", "max"],
    accountsTotal: 0,
    accountsAvailable: 0,
    needsLogin: true,
  },
  {
    id: "devin",
    label: "Devin",
    models: ["devin-latest"],
    efforts: ["low", "medium", "high"],
    accountsTotal: 1,
    accountsAvailable: 1,
    needsLogin: false,
  },
];

function activity(seq: number, partial: Partial<import("./types").ActivityItem>) {
  return {
    id: `fx-act-${seq}`,
    seq,
    ts: iso(60_000 - seq * 1000),
    turnId: "fx-t1",
    kind: "info" as const,
    ...partial,
  };
}

const finishedTurn: Turn = {
  id: "fx-t1",
  index: 1,
  prompt: "Add a health-check endpoint to the session service",
  status: "finished",
  createdAt: iso(3_600_000),
  startedAt: iso(3_590_000),
  finishedAt: iso(3_300_000),
  result:
    "Added `GET /healthz` returning `{status:'ok', uptime_s}` with a unit test covering the happy path.",
  error: null,
  activity: [
    activity(1, { kind: "status", status: "running" }),
    activity(2, {
      kind: "reasoning",
      text: "Locating the service entrypoint and existing route registration…",
    }),
    activity(3, {
      kind: "command",
      command: "rg -n \"add_route|@app\" control/",
      exitCode: 0,
      output: "control/service.py:42",
    }),
    activity(4, {
      kind: "file_change",
      path: "control/service.py",
      changeType: "modified",
    }),
    activity(5, {
      kind: "file_change",
      path: "tests/unit/test_healthz.py",
      changeType: "added",
    }),
    activity(6, {
      kind: "command",
      command: "uv run pytest tests/unit/test_healthz.py",
      exitCode: 0,
      output: "1 passed in 0.42s",
    }),
    activity(7, {
      kind: "message",
      role: "assistant",
      text: "Added `GET /healthz` returning `{status:'ok', uptime_s}` with a unit test covering the happy path.",
    }),
    activity(8, { kind: "status", status: "finished" }),
  ],
};

const runningTurn: Turn = {
  id: "fx-t2",
  index: 1,
  prompt: "Migrate the settings store to the new schema",
  status: "running",
  createdAt: iso(90_000),
  startedAt: iso(80_000),
  finishedAt: null,
  result: null,
  error: null,
  activity: [
    activity(20, { kind: "status", status: "running", turnId: "fx-t2" }),
    activity(21, {
      kind: "reasoning",
      turnId: "fx-t2",
      text: "Reading current settings schema before writing the migration…",
    }),
    activity(22, {
      kind: "command",
      turnId: "fx-t2",
      command: "git status --short",
      exitCode: 0,
      output: "",
    }),
  ],
};

export const SESSIONS: Session[] = [
  {
    id: "sess-8f3a1c",
    title: "Add a health-check endpoint to the session service",
    phase: "idle",
    endReason: null,
    provider: "codex",
    model: "gpt-5-codex",
    accountLabel: "codex/work",
    repo: { name: "soren-labs/sbx-browser", ref: "main" },
    effort: "medium",
    compute: { cpu: [1, 2], memoryMib: [1024, 8192] },
    idleTimeoutS: 1800,
    delivery: { mode: "pr" },
    createdAt: iso(3_600_000),
    updatedAt: iso(3_300_000),
    usage: {
      inputTokens: 42_318,
      cachedInputTokens: 30_112,
      outputTokens: 3_912,
      reasoningOutputTokens: 1_204,
    },
    costUsd: 0.42,
    runtimeSeconds: 318,
    turns: [finishedTurn],
    lastActivityPreview: "1 passed in 0.42s",
    hasChanges: true,
    error: null,
  },
  {
    id: "sess-2b7d9e",
    title: "Migrate the settings store to the new schema",
    phase: "running",
    endReason: null,
    provider: "grok",
    model: "grok-code-fast",
    accountLabel: "grok/main",
    repo: { name: "soren-labs/sbx-browser", ref: "main" },
    effort: "high",
    compute: { cpu: [2, 4], memoryMib: [4096, 16384] },
    idleTimeoutS: 3600,
    delivery: { mode: "branch" },
    createdAt: iso(95_000),
    updatedAt: iso(20_000),
    usage: { inputTokens: 12_004, cachedInputTokens: 8_200, outputTokens: 640 },
    costUsd: 0.11,
    runtimeSeconds: 74,
    turns: [runningTurn],
    lastActivityPreview: "Reading current settings schema…",
    hasChanges: false,
    error: null,
  },
  {
    id: "sess-6c1e07",
    title: "Prototype dark-mode tokens for the console",
    phase: "queued",
    endReason: null,
    provider: "codex",
    model: "gpt-5",
    accountLabel: "codex/personal",
    repo: null,
    effort: "low",
    compute: null,
    idleTimeoutS: null,
    delivery: { mode: "none" },
    createdAt: iso(25_000),
    updatedAt: iso(24_000),
    usage: null,
    costUsd: null,
    runtimeSeconds: null,
    turns: [],
    lastActivityPreview: null,
    hasChanges: false,
    error: null,
  },
  {
    id: "sess-91ad44",
    title: "Explain the retry backoff in the SSE client",
    phase: "ended",
    endReason: "closed",
    provider: "devin",
    model: "devin-latest",
    accountLabel: "devin/team",
    repo: { name: "soren-labs/sbx-browser" },
    effort: null,
    compute: { cpu: [1, 2], memoryMib: [1024, 8192] },
    idleTimeoutS: 900,
    delivery: { mode: "none" },
    createdAt: iso(86_400_000),
    updatedAt: iso(80_000_000),
    usage: { inputTokens: 9_100, cachedInputTokens: 5_400, outputTokens: 1_840 },
    costUsd: 0.09,
    runtimeSeconds: 240,
    turns: [
      {
        id: "fx-t9",
        index: 1,
        prompt: "Explain the retry backoff in the SSE client",
        status: "finished",
        createdAt: iso(86_400_000),
        startedAt: iso(86_300_000),
        finishedAt: iso(80_000_000),
        result:
          "The client retries with exponential backoff capped at 30 s and resumes from Last-Event-ID.",
        error: null,
        activity: [
          activity(40, {
            kind: "message",
            turnId: "fx-t9",
            role: "assistant",
            text: "The client retries with exponential backoff capped at 30 s and resumes from Last-Event-ID.",
          }),
        ],
      },
    ],
    lastActivityPreview: "Backoff capped at 30 s",
    hasChanges: false,
    error: null,
  },
  {
    id: "sess-d4e5f6",
    title: "Regenerate provider icons",
    phase: "failed",
    endReason: "failed",
    provider: "antigravity",
    model: "agy-pro",
    accountLabel: "agy/main",
    repo: null,
    effort: "medium",
    compute: null,
    idleTimeoutS: null,
    delivery: { mode: "none" },
    createdAt: iso(200_000),
    updatedAt: iso(190_000),
    usage: null,
    costUsd: null,
    runtimeSeconds: null,
    turns: [
      {
        id: "fx-t10",
        index: 1,
        prompt: "Regenerate provider icons",
        status: "error",
        createdAt: iso(200_000),
        startedAt: iso(199_000),
        finishedAt: iso(190_000),
        result: null,
        error: {
          code: "auth_invalid",
          source: "provider",
          message: "Provider rejected the stored credential",
          retryable: false,
        },
        activity: [
          activity(50, {
            kind: "error",
            turnId: "fx-t10",
            error: {
              code: "auth_invalid",
              source: "provider",
              message: "Provider rejected the stored credential",
              retryable: false,
            },
          }),
        ],
      },
    ],
    lastActivityPreview: "Provider rejected the stored credential",
    hasChanges: false,
    error: {
      code: "auth_invalid",
      source: "provider",
      message: "Provider rejected the stored credential",
      retryable: false,
    },
  },
];

export const CHANGES: Record<string, SessionChange[]> = {
  "sess-8f3a1c": [
    {
      id: "chg-1",
      kind: "file",
      summary: "control/service.py — health route added",
      ts: iso(3_400_000),
      path: "control/service.py",
      changeType: "modified",
    },
    {
      id: "chg-2",
      kind: "file",
      summary: "tests/unit/test_healthz.py — new coverage",
      ts: iso(3_400_000),
      path: "tests/unit/test_healthz.py",
      changeType: "added",
    },
    {
      id: "chg-3",
      kind: "revision",
      summary: "Revision r2 ready for review",
      ts: iso(3_320_000),
      ref: "r2",
    },
    {
      id: "chg-4",
      kind: "delivery",
      summary: "Draft PR opened: soren-labs/sbx-browser#97",
      ts: iso(3_300_000),
      url: "https://github.com/soren-labs/sbx-browser/pull/97",
    },
  ],
};

export const INTEGRATIONS: IntegrationStatus = {
  providers: PROVIDERS,
  github: {
    configured: true,
    connected: true,
    account: "soren-labs",
  },
  runtime: { enabled: true, backend: "modal" },
};

/** Scripted lifecycle a mock-created session follows (ms delays). */
export const CREATE_SCRIPT: { delayMs: number; phase: Session["phase"] }[] = [
  { delayMs: 0, phase: "queued" },
  { delayMs: 700, phase: "starting" },
  { delayMs: 1400, phase: "running" },
];

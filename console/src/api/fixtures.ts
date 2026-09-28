import type {
  IntegrationStatus,
  ModelInfo,
  ProviderInfo,
  Session,
  SessionChange,
  SessionChangesDiff,
  Turn,
} from "./types";

/**
 * Typed local fixtures for the V2 console. Product-shaped (what the UI
 * consumes after http.ts/normalize.ts mapping) — every value mirrors the
 * real /v2 + /v1 wire shapes; never real credentials.
 */

const now = Date.now();
const iso = (msAgo: number) => new Date(now - msAgo).toISOString();

export const PROVIDERS: ProviderInfo[] = [
  {
    id: "codex",
    label: "Codex",
    support: "full",
    readiness: "ready",
    models: ["gpt-5-codex", "gpt-5", "o4-mini"],
    runtimeStatus: "ready",
    runtimeEnabled: true,
    connectionStatus: "connected",
    accountsTotal: 2,
    accountsAvailable: 1,
    needsLogin: false,
  },
  {
    id: "antigravity",
    label: "Antigravity",
    support: "full",
    readiness: "busy",
    models: ["agy-pro", "agy-standard"],
    runtimeStatus: "ready",
    runtimeEnabled: true,
    connectionStatus: "connected",
    accountsTotal: 1,
    accountsAvailable: 0,
    needsLogin: false,
  },
  {
    id: "grok",
    label: "Grok",
    support: "full",
    readiness: "ready",
    models: ["grok-code-fast", "grok-4"],
    runtimeStatus: "ready",
    runtimeEnabled: true,
    connectionStatus: "connected",
    accountsTotal: 1,
    accountsAvailable: 1,
    needsLogin: false,
  },
  {
    id: "opencode",
    label: "OpenCode",
    support: "full",
    readiness: "needs_login",
    models: ["oc-sonnet", "oc-haiku"],
    runtimeStatus: "degraded",
    runtimeEnabled: true,
    connectionStatus: "not_connected",
    connectionDetail: "credential file missing",
    accountsTotal: 0,
    accountsAvailable: 0,
    needsLogin: true,
  },
  {
    id: "devin",
    label: "Devin",
    support: "full",
    readiness: "ready",
    models: ["devin-latest"],
    runtimeStatus: "ready",
    runtimeEnabled: true,
    connectionStatus: "connected",
    accountsTotal: 1,
    accountsAvailable: 1,
    needsLogin: false,
  },
];

/** /v1/models-shaped rows — the only fixture surface with real account ids. */
export const MODELS: ModelInfo[] = [
  {
    provider: "codex",
    model: "gpt-5-codex",
    displayName: "GPT-5 Codex",
    account: "codex-work",
    accountsAvailable: 1,
    availability: "available",
    reasoningEfforts: ["minimal", "low", "medium", "high"],
    defaultEffort: "medium",
  },
  {
    provider: "codex",
    model: "gpt-5",
    displayName: "GPT-5",
    account: "codex-personal",
    accountsAvailable: 0,
    availability: "busy",
    reasoningEfforts: ["low", "medium", "high", "xhigh"],
    defaultEffort: "medium",
  },
  {
    provider: "grok",
    model: "grok-code-fast",
    account: "grok-main",
    accountsAvailable: 1,
    availability: "available",
    reasoningEfforts: ["low", "medium", "high"],
  },
  {
    provider: "antigravity",
    model: "agy-pro",
    account: "agy-main",
    accountsAvailable: 0,
    availability: "busy",
    reasoningEfforts: ["medium", "high", "xhigh"],
    defaultEffort: "high",
  },
  {
    provider: "devin",
    model: "devin-latest",
    account: "devin-team",
    accountsAvailable: 1,
    availability: "available",
    reasoningEfforts: ["low", "medium", "high"],
    defaultEffort: "medium",
  },
];

function activity(
  seq: number,
  partial: Partial<import("./types").ActivityItem>,
): import("./types").ActivityItem {
  return {
    id: `fx-act-${seq}`,
    seq,
    ts: iso(60_000 - seq * 1000),
    turnId: "turn-1",
    kind: "info" as const,
    ...partial,
  };
}

const finishedTurn: Turn = {
  id: "turn-1",
  index: 1,
  prompt: "Add a health-check endpoint to the session service",
  status: "finished",
  createdAt: iso(3_600_000),
  startedAt: iso(3_590_000),
  finishedAt: iso(3_300_000),
  result:
    "Added `GET /healthz` returning `{status:'ok', uptime_s}` with a unit test covering the happy path.",
  error: null,
  usage: { inputTokens: 42_318, cachedInputTokens: 30_112, outputTokens: 3_912 },
  provider: "codex",
  model: "gpt-5-codex",
  effort: "medium",
  activity: [
    activity(1, { kind: "status", status: "running", n: 1 }),
    activity(2, {
      kind: "reasoning",
      n: 1,
      text: "Locating the service entrypoint and existing route registration…",
    }),
    activity(3, {
      kind: "command",
      n: 1,
      command: "rg -n \"add_route|@app\" control/",
      exitCode: 0,
      output: "control/service.py:42",
    }),
    activity(4, {
      kind: "file_change",
      n: 1,
      path: "control/service.py",
      changeType: "modified",
      changes: [{ path: "control/service.py", kind: "modified" }],
    }),
    activity(5, {
      kind: "file_change",
      n: 1,
      path: "tests/unit/test_healthz.py",
      changeType: "added",
      changes: [{ path: "tests/unit/test_healthz.py", kind: "added" }],
    }),
    activity(6, {
      kind: "command",
      n: 1,
      command: "uv run pytest tests/unit/test_healthz.py",
      exitCode: 0,
      output: "1 passed in 0.42s",
    }),
    activity(7, {
      kind: "message",
      n: 1,
      role: "assistant",
      text: "Added `GET /healthz` returning `{status:'ok', uptime_s}` with a unit test covering the happy path.",
    }),
    activity(8, { kind: "status", status: "finished", n: 1 }),
  ],
};

const runningTurn: Turn = {
  id: "turn-1",
  index: 1,
  prompt: "Migrate the settings store to the new schema",
  status: "running",
  createdAt: iso(90_000),
  startedAt: iso(80_000),
  finishedAt: null,
  result: null,
  error: null,
  provider: "grok",
  model: "grok-code-fast",
  effort: "high",
  activity: [
    activity(20, { kind: "status", status: "running", n: 1 }),
    activity(21, {
      kind: "reasoning",
      n: 1,
      text: "Reading current settings schema before writing the migration…",
    }),
    activity(22, {
      kind: "command",
      n: 1,
      command: "git status --short",
      exitCode: 0,
      output: "",
    }),
  ],
};

const baseSession = {
  accountLabel: null,
  effort: null,
  compute: null,
  idleTimeoutS: null,
  delivery: null,
  usage: null,
  costUsd: null,
  lastActivityPreview: null,
  hasChanges: false,
  error: null,
};

export const SESSIONS: Session[] = [
  {
    ...baseSession,
    id: "sess-8f3a1c2b9d4e5f01",
    title: "Add a health-check endpoint to the session service",
    prompt: "Add a health-check endpoint to the session service",
    status: "finished",
    phase: "idle",
    endReason: null,
    provider: "codex",
    model: "gpt-5-codex",
    repo: {
      name: "soren-labs/sbx-browser",
      url: "https://github.com/soren-labs/sbx-browser",
      ref: "main",
      baseSha: "3b6db744ba622ebf5eb6cdd4a5f238aaae2b16c9",
    },
    effort: "medium",
    delivery: {
      mode: "pr",
      status: "delivered",
      branch: "sbx/sess-8f3a1c2b-1",
      // A follow-up turn produced a newer head than the PR holds —
      // exercises the "new changes → update same PR" path.
      pushedHeadSha: "9e57deadbeef0123456789abcdef0123456789",
      prUrl: "https://github.com/soren-labs/sbx-browser/pull/97",
      prNumber: 97,
      prState: "open",
      prHeadSha: "9e57deadbeef0123456789abcdef0123456789",
      prBase: "main",
    },
    changes: {
      status: "ready",
      baseSha: "3b6db744ba622ebf5eb6cdd4a5f238aaae2b16c9",
      headSha: "cafe0123deadbeef4567890abcdef012345678",
      branch: "sbx/sess-8f3a1c2b-1",
    },
    createdAt: iso(3_600_000),
    updatedAt: iso(3_300_000),
    usage: {
      inputTokens: 42_318,
      cachedInputTokens: 30_112,
      outputTokens: 3_912,
      reasoningOutputTokens: 1_204,
    },
    costUsd: 0.42,
    turnCount: 1,
    turns: [finishedTurn],
    lastActivityPreview: "1 passed in 0.42s",
    hasChanges: true,
  },
  {
    ...baseSession,
    id: "sess-2b7d9e4a5c6f7081",
    title: "Migrate the settings store to the new schema",
    prompt: "Migrate the settings store to the new schema",
    status: "running",
    phase: "running",
    endReason: null,
    provider: "grok",
    model: "grok-code-fast",
    repo: {
      name: "soren-labs/sbx-browser",
      url: "https://github.com/soren-labs/sbx-browser",
      ref: "main",
    },
    effort: "high",
    idleTimeoutS: 3600,
    delivery: null,
    createdAt: iso(95_000),
    updatedAt: iso(20_000),
    usage: { inputTokens: 12_004, cachedInputTokens: 8_200, outputTokens: 640 },
    costUsd: 0.11,
    turnCount: 1,
    turns: [runningTurn],
    lastActivityPreview: "Reading current settings schema…",
  },
  {
    ...baseSession,
    id: "sess-6c1e07f8a9b0c1d2",
    title: "Prototype dark-mode tokens for the console",
    prompt: "Prototype dark-mode tokens for the console",
    status: "queued",
    phase: "queued",
    endReason: null,
    provider: null,
    model: null,
    repo: null,
    effort: null,
    createdAt: iso(25_000),
    updatedAt: iso(24_000),
    turnCount: 0,
    turns: [],
  },
  {
    ...baseSession,
    id: "sess-91ad44b2c3d4e5f6",
    title: "Explain the retry backoff in the SSE client",
    prompt: "Explain the retry backoff in the SSE client",
    status: "cancelled",
    phase: "ended",
    endReason: "cancelled",
    provider: "devin",
    model: "devin-latest",
    repo: {
      name: "soren-labs/sbx-browser",
      url: "https://github.com/soren-labs/sbx-browser",
    },
    createdAt: iso(86_400_000),
    updatedAt: iso(80_000_000),
    usage: { inputTokens: 9_100, cachedInputTokens: 5_400, outputTokens: 1_840 },
    costUsd: 0.09,
    turnCount: 1,
    turns: [
      {
        id: "turn-1",
        index: 1,
        prompt: "Explain the retry backoff in the SSE client",
        status: "cancelled",
        createdAt: iso(86_400_000),
        startedAt: iso(86_300_000),
        finishedAt: iso(80_000_000),
        result:
          "The client retries with exponential backoff capped at 30 s and resumes from Last-Event-ID.",
        error: null,
        activity: [
          activity(40, {
            kind: "message",
            n: 1,
            role: "assistant",
            text: "The client retries with exponential backoff capped at 30 s and resumes from Last-Event-ID.",
          }),
        ],
      },
    ],
    lastActivityPreview: "Backoff capped at 30 s",
  },
  {
    ...baseSession,
    id: "sess-d4e5f6a7b8c9d0e1",
    title: "Regenerate provider icons",
    prompt: "Regenerate provider icons",
    status: "failed",
    phase: "failed",
    endReason: "failed",
    provider: "antigravity",
    model: "agy-pro",
    repo: null,
    effort: "medium",
    createdAt: iso(200_000),
    updatedAt: iso(190_000),
    turnCount: 1,
    turns: [
      {
        id: "turn-1",
        index: 1,
        prompt: "Regenerate provider icons",
        status: "failed",
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
            n: 1,
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
    error: {
      code: "auth_invalid",
      source: "provider",
      message: "Provider rejected the stored credential",
      retryable: false,
    },
  },
  {
    ...baseSession,
    id: "sess-aa00ff1122aabbcc",
    title: "Fix the flaky session list filter",
    prompt: "Fix the flaky session list filter",
    status: "finished",
    phase: "idle",
    endReason: null,
    provider: "devin",
    model: "devin-latest",
    repo: {
      name: "soren-labs/sbx-browser",
      url: "https://github.com/soren-labs/sbx-browser",
      ref: "main",
      baseSha: "3b6db744ba622ebf5eb6cdd4a5f238aaae2b16c9",
    },
    delivery: null,
    changes: {
      status: "ready",
      baseSha: "3b6db744ba622ebf5eb6cdd4a5f238aaae2b16c9",
      headSha: "f00dbabe1234567890abcdef0123456789ab",
    },
    createdAt: iso(7_200_000),
    updatedAt: iso(7_000_000),
    usage: { inputTokens: 9_410, cachedInputTokens: 5_001, outputTokens: 1_204 },
    costUsd: 0.09,
    turnCount: 1,
    turns: [
      {
        id: "turn-1",
        index: 1,
        prompt: "Fix the flaky session list filter",
        status: "finished",
        createdAt: iso(7_200_000),
        startedAt: iso(7_199_000),
        finishedAt: iso(7_000_000),
        result: "Filter now compares normalized ids only.",
        error: null,
        activity: [
          activity(60, {
            kind: "file_change",
            n: 1,
            changes: [
              { path: "console/src/pages/SessionsPage.tsx", kind: "modified" },
            ],
          }),
        ],
      },
    ],
    lastActivityPreview: "Filter now compares normalized ids only.",
    hasChanges: true,
  },
];

export const CHANGES: Record<string, SessionChange[]> = {
  "sess-8f3a1c2b9d4e5f01": [
    {
      id: "workspace",
      kind: "workspace",
      status: "ready",
      summary: "workspace changes ready",
      ts: iso(3_400_000),
      branch: "sbx/sess-8f3a1c2b-1",
      headSha: "cafe0123deadbeef4567890abcdef012345678",
      url: "https://github.com/soren-labs/sbx-browser/pull/97",
    },
    {
      id: "rev-1",
      kind: "revision",
      n: 1,
      status: "ready",
      deliveryStatus: "delivered",
      summary: "revision 1",
      ts: iso(3_320_000),
      branch: "sbx/sess-8f3a1c2b-1",
      headSha: "cafe0123deadbeef4567890abcdef012345678",
      url: "https://github.com/soren-labs/sbx-browser/pull/97",
      prNumber: 97,
    },
  ],
  "sess-aa00ff1122aabbcc": [
    {
      id: "workspace",
      kind: "workspace",
      status: "ready",
      summary: "workspace changes ready",
      ts: iso(7_000_000),
      headSha: "f00dbabe1234567890abcdef0123456789ab",
    },
    {
      id: "rev-1",
      kind: "revision",
      n: 1,
      status: "ready",
      summary: "revision 1",
      ts: iso(7_000_000),
      headSha: "f00dbabe1234567890abcdef0123456789ab",
    },
  ],
};

/** File-level views served by GET .../changes/diff (stats, no bodies). */
export const DIFFS: Record<string, SessionChangesDiff> = {
  "sess-8f3a1c2b9d4e5f01": {
    n: 1,
    baseSha: "3b6db744ba622ebf5eb6cdd4a5f238aaae2b16c9",
    headSha: "cafe0123deadbeef4567890abcdef012345678",
    filesChanged: 3,
    additions: 19,
    deletions: 4,
    files: [
      {
        path: "control/api_v2/routes.py",
        status: "modified",
        additions: 11,
        deletions: 2,
      },
      { path: "control/api_v2/health.py", status: "added", additions: 8, deletions: 0 },
      {
        path: "docs/contracts/api-v2.md",
        status: "modified",
        additions: 0,
        deletions: 2,
      },
    ],
  },
  "sess-aa00ff1122aabbcc": {
    n: 1,
    baseSha: "3b6db744ba622ebf5eb6cdd4a5f238aaae2b16c9",
    headSha: "f00dbabe1234567890abcdef0123456789ab",
    filesChanged: 2,
    additions: 9,
    deletions: 1,
    files: [
      {
        path: "console/src/pages/SessionsPage.tsx",
        status: "modified",
        additions: 7,
        deletions: 1,
      },
      {
        path: "console/src/components/SessionCard.tsx",
        status: "modified",
        additions: 2,
        deletions: 0,
      },
    ],
  },
};

/** Per-file diff bodies served lazily by GET .../changes/diff?path=. */
export const FILE_DIFFS: Record<string, Record<string, string>> = {
  "sess-8f3a1c2b9d4e5f01": {
    "control/api_v2/routes.py":
      "diff --git a/control/api_v2/routes.py b/control/api_v2/routes.py\n" +
      "index 111aaaa..222bbbb 100644\n" +
      "--- a/control/api_v2/routes.py\n" +
      "+++ b/control/api_v2/routes.py\n" +
      "@@ -560,6 +560,15 @@ def session_changes(\n" +
      "     rows = revisions.list(record.agent_id) if record.agent_id else []\n" +
      "+\n" +
      "+\n" +
      "+@router.get(\"/sessions/{session_id}/healthz\")\n" +
      "+def session_healthz(session_id: str):\n" +
      "+    return {\"status\": \"ok\"}\n" +
      "+\n" +
      "+\n" +
      "-@router.post(\"/sessions/{session_id}/legacy-health\")\n" +
      "-def legacy_health():\n" +
      "     return {\"session\": session, \"changes\": session[\"changes\"]}",
    "control/api_v2/health.py":
      "diff --git a/control/api_v2/health.py b/control/api_v2/health.py\n" +
      "new file mode 100644\n" +
      "index 0000000..333cccc\n" +
      "--- /dev/null\n" +
      "+++ b/control/api_v2/health.py\n" +
      "@@ -0,0 +1,8 @@\n" +
      "+\"\"\"Liveness probe.\"\"\"\n" +
      "+\n" +
      "+OK = {\"status\": \"ok\"}\n" +
      "+\n" +
      "+\n" +
      "+def healthz():\n" +
      "+    return OK\n" +
      "+",
    "docs/contracts/api-v2.md":
      "diff --git a/docs/contracts/api-v2.md b/docs/contracts/api-v2.md\n" +
      "index 444dddd..555eeee 100644\n" +
      "--- a/docs/contracts/api-v2.md\n" +
      "+++ b/docs/contracts/api-v2.md\n" +
      "@@ -8,8 +8,6 @@\n" +
      " ## Endpoints\n" +
      "-\n" +
      "-TODO: document healthz\n" +
      " - `GET /v2/sessions`\n",
  },
  "sess-aa00ff1122aabbcc": {
    "console/src/pages/SessionsPage.tsx":
      "diff --git a/console/src/pages/SessionsPage.tsx b/console/src/pages/SessionsPage.tsx\n" +
      "index 666ffff..7770000 100644\n" +
      "--- a/console/src/pages/SessionsPage.tsx\n" +
      "+++ b/console/src/pages/SessionsPage.tsx\n" +
      "@@ -40,7 +40,7 @@ export function SessionsPage() {\n" +
      "   const filtered = sessions.filter((s) =>\n" +
      "-    s.id.includes(query)\n" +
      "+    s.id.toLowerCase().includes(query.toLowerCase())\n" +
      "   );\n" +
      "@@ -60,6 +60,12 @@ export function SessionsPage() {\n" +
      "+  useEffect(() => {\n" +
      "+    if (query) setFiltered(filtered);\n" +
      "+  }, [query]);\n" +
      "+\n" +
      "+  const norm = (v: string) => v.trim().toLowerCase();\n" +
      "+\n" +
      "   return (\n",
    "console/src/components/SessionCard.tsx":
      "diff --git a/console/src/components/SessionCard.tsx b/console/src/components/SessionCard.tsx\n" +
      "index 8881111..9992222 100644\n" +
      "--- a/console/src/components/SessionCard.tsx\n" +
      "+++ b/console/src/components/SessionCard.tsx\n" +
      "@@ -12,6 +12,8 @@ export function SessionCard({ session }) {\n" +
      "+  const badge = session.hasChanges ? \"changes\" : null;\n" +
      "+\n" +
      "   return (\n",
  },
};

export const INTEGRATIONS: IntegrationStatus = {
  providers: PROVIDERS,
  github: {
    configured: true,
    installable: true,
    connected: true,
    accounts: ["soren-labs"],
    appSlug: "sbx-browser",
    source: "env",
  },
  runtime: { enabled: true, backend: "local" },
};

/** Scripted lifecycle a fixture-created session follows (ms delays). */
export const CREATE_SCRIPT: { delayMs: number; phase: Session["phase"] }[] = [
  { delayMs: 0, phase: "queued" },
  { delayMs: 700, phase: "starting" },
  { delayMs: 1400, phase: "running" },
];

export type Page<T> = { items: T[]; next_cursor?: string | null };
export type User = {
  id: string;
  email: string;
  verified_at: string | null;
  workspace_ids: string[];
};
export type Connection = {
  id: string;
  kind: "modal" | "github" | "opencode_zen";
  state: string;
  health: string;
  version: number;
  label: string;
};
export type Model = {
  id: string;
  name: string;
  free: boolean;
  connection_id: string;
  availability: string;
};
export type Project = {
  id: string;
  name: string;
  current_version_id: string;
  version: number;
};
export type Turn = {
  id: string;
  ordinal: number;
  state: string;
  evidence_complete: boolean;
  outcome?: { text?: string };
  reason?: string;
};
export type Message = { id: string; content: string; ordinal: number };
export type Part = {
  id: string;
  kind: string;
  revision: number;
  content: string;
  execution_id: string;
};
export type Session = {
  id: string;
  title: string;
  version: number;
  lifecycle: string;
  provider_id: string;
  workspace_id: string;
  messages: Message[];
  turns: Turn[];
  parts: Part[];
  event_watermark: number;
  worktree: {
    id: string;
    generation: number;
    availability: string;
    last_snapshot_id: string | null;
  };
};
export type Event = {
  id: string;
  seq: number;
  type: string;
  schema_version: number;
  payload: Record<string, unknown>;
  recorded_at: string;
};
export type Changeset = {
  id: string;
  state: string;
  generation: number;
  subject_digest: string;
  head_sha: string | null;
  manifest?: {
    subject: { files: { path: string; type: string; mode: string }[] };
  };
};
export type Delivery = {
  id: string;
  version: number;
  state: string;
  subject_digest: string;
  mapped_head: string;
  pr_url?: string;
  reason?: string;
  steps: { kind: string; evidence: unknown }[];
  merge_requests: { id: string; state: string; evidence: unknown }[];
};
export type Delegation = {
  id: string;
  role: string;
  child_session_id: string;
  state: string;
  result?: {
    verdict: string;
    validated: boolean;
    subject_digest: string;
    value: unknown;
  };
};
export type Operation = { operation_id: string; job_id: string };

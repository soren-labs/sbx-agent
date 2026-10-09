import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { ChangeSet, Delivery } from "../api/types";
import { ChangesPanel } from "../features/changes/ChangesPanel";
import { DeliveryCard } from "../features/changes/DeliveryCard";
import { identityRoutes, mockFetch, renderApp } from "../test/helpers";

const delivery = (over: Partial<Delivery> = {}): Delivery => ({
  id: "d1",
  session_id: "s1",
  changeset_id: "cs1",
  subject_digest: "sha256:subject",
  transport: "pull_request",
  repository: "acme/app",
  target_ref: "sbx/x",
  base_branch: "main",
  draft: false,
  state: "succeeded",
  state_reason: null,
  commit_sha: "c0ffee1234567890",
  pull_request: { number: 7, url: "https://github.com/acme/app/pull/7", draft: false, state: "open" },
  steps: [{ kind: "push", outcome: "succeeded" }],
  merge_requests: [],
  merge_eligibility: { eligible: true, reasons: [], subject_digest: "sha256:subject", head_sha: "c0ffee1234567890", observed_at: "2026-10-05T00:00:00Z" },
  version: 4,
  ...over,
});

const cs: ChangeSet = {
  id: "cs1",
  session_id: "s1",
  source_turn_id: null,
  state: "ready",
  origin: "explicit",
  subject_digest: "sha256:subject",
  repository: "acme/app",
  base_sha: "b",
  head_sha: "h",
  worktree_generation: 1,
  file_count: 1,
  error: null,
  created_at: "2026-10-05T00:00:00Z",
  sealed_at: "2026-10-05T00:00:01Z",
};

describe("Delivery merge gate", () => {
  it("sends exactly the server-provided pins", async () => {
    const m = mockFetch([...identityRoutes, ["POST", "/api/deliveries/d1/merge-requests", { status: 202, json: { merge_request_id: "m1" } }]]);
    let changed = 0;
    await renderApp(<DeliveryCard delivery={delivery()} onChanged={() => changed++} />, m.fetch);
    expect(screen.getByText("Eligible to merge")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Merge (squash)" }));
    await waitFor(() => expect(changed).toBe(1));
    const [req] = m.find("POST", "/api/deliveries/d1/merge-requests");
    expect(req.body).toEqual({ expected_head_sha: "c0ffee1234567890", subject_digest: "sha256:subject", expected_version: 4, method: "squash" });
    expect(req.headers["Idempotency-Key"]).toBeTruthy();
  });

  it("includes mark_ready only when the user opts in for a draft PR", async () => {
    const m = mockFetch([...identityRoutes, ["POST", "/api/deliveries/d1/merge-requests", { status: 202, json: {} }]]);
    const d = delivery({ pull_request: { number: 7, url: "https://github.com/acme/app/pull/7", draft: true, state: "open" } });
    await renderApp(<DeliveryCard delivery={d} onChanged={() => undefined} />, m.fetch);
    await userEvent.click(screen.getByLabelText("Mark ready for review"));
    await userEvent.click(screen.getByRole("button", { name: "Merge (squash)" }));
    await waitFor(() => expect(m.find("POST", "/api/deliveries/d1/merge-requests")).toHaveLength(1));
    expect(m.find("POST", "/api/deliveries/d1/merge-requests")[0].body).toMatchObject({ mark_ready: true });
  });

  it("shows server reasons and does not offer a merge when not eligible", async () => {
    const m = mockFetch(identityRoutes);
    const d = delivery({
      state: "failed",
      merge_eligibility: { eligible: false, reasons: ["missing_required_result:ReviewAssessment", "observation_stale"], subject_digest: "sha256:subject", head_sha: "c0ffee1234567890", observed_at: null },
    });
    await renderApp(<DeliveryCard delivery={d} onChanged={() => undefined} />, m.fetch);
    expect(screen.getByText("Not eligible to merge")).toBeInTheDocument();
    // The server's reason codes are explained in words; the code stays available on hover.
    const reason = screen.getByTitle("missing_required_result:ReviewAssessment");
    expect(reason).toHaveTextContent("A required review result is missing: ReviewAssessment");
    expect(screen.getByTitle("observation_stale")).toHaveTextContent("Remote state has not been refreshed yet");
    expect(screen.getByText("observation_stale")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Merge (squash)" })).toBeDisabled();
    // retry names what is retried
    expect(screen.getByRole("button", { name: "Retry Delivery" })).toBeInTheDocument();
    expect(m.find("POST", /merge-requests/)).toHaveLength(0);
  });
});

describe("Changes panel", () => {
  it("renders a diagnosis for executor_unavailable and never activates compute", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/changes", { status: 409, json: { error: { code: "executor_unavailable", category: "availability", message: "runtime unreachable", retryable: true } } }],
      ["GET", "/api/sessions/s1/changesets", { json: { items: [cs] } }],
      ["GET", "/api/changesets/cs1", { json: { ...cs, files: [{ path: "src/a.ts", type: "modified" }] } }],
      ["GET", "/api/changesets/cs1/deliveries", { json: { items: [delivery()] } }],
    ]);
    await renderApp(<ChangesPanel sessionId="s1" />, m.fetch);
    expect(await screen.findByText("Compute is not running")).toBeInTheDocument();
    expect(await screen.findByText("src/a.ts")).toBeInTheDocument();
    expect(await screen.findByText("Eligible to merge")).toBeInTheDocument();
    expect(m.calls.filter((c) => c.method !== "GET")).toHaveLength(0);
  });

  it("lazily loads the diff and captures a ChangeSet with an Idempotency-Key", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/changes", { json: { live: true, observation: { files: [{ path: "x.ts", status: "M" }] } } }],
      ["GET", "/api/sessions/s1/changesets", { json: { items: [cs] } }],
      ["GET", "/api/changesets/cs1", { json: { ...cs, files: [] } }],
      ["GET", "/api/changesets/cs1/deliveries", { json: { items: [] } }],
      ["GET", "/api/changesets/cs1/diff", { json: { diff: "@@ -1 +1 @@\n-a\n+b" } }],
      ["POST", "/api/sessions/s1/changesets", { status: 202, json: { changeset: cs, event_watermark: 5 } }],
    ]);
    await renderApp(<ChangesPanel sessionId="s1" />, m.fetch);
    expect(await screen.findByText("x.ts")).toBeInTheDocument();
    expect(m.find("GET", "/api/changesets/cs1/diff")).toHaveLength(0);
    await userEvent.click(await screen.findByRole("button", { name: "Show diff" }));
    expect(await screen.findByLabelText("Diff")).toHaveTextContent("+b");
    await userEvent.click(screen.getByRole("button", { name: "Capture ChangeSet" }));
    await waitFor(() => expect(m.find("POST", "/api/sessions/s1/changesets")).toHaveLength(1));
    const [post] = m.find("POST", "/api/sessions/s1/changesets");
    expect(post.body).toEqual({ origin: "explicit" });
    expect(post.headers["Idempotency-Key"]).toBeTruthy();
  });
});

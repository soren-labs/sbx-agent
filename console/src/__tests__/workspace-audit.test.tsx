import { act, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import type { ChangeSet } from "../api/types";
import { SessionPage } from "../features/sessions/SessionPage";
import { ChangesTab } from "../features/sessions/tabs/ChangesTab";
import { FilesTab } from "../features/sessions/tabs/FilesTab";
import { TerminalTab, resetTerminalViews } from "../features/sessions/tabs/TerminalTab";
import type { SessionLive } from "../state/session-live";
import { identityRoutes, mockFetch, renderApp, session, type RecordedCall } from "../test/helpers";

const live = { refresh: () => undefined } as unknown as SessionLive;
const notFound = { status: 404, json: { error: { code: "not_found", category: "access", message: "file not found", retryable: false } } };
const err = (status: number, code: string, message = code) => ({ status, json: { error: { code, category: "x", message, retryable: false } } });

describe("Files: open-or-create and partial views (AUD02/AUD04)", () => {
  it("opens a blank editor for a missing path and creates it with expected_digest absent", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/files", { json: { items: [] } }],
      ["GET", "/api/sessions/s1/files/content", notFound],
      ["PUT", "/api/sessions/s1/files", { json: { path: "docs/new.txt", generation: 2, digest: "sha256:x" } }],
    ]);
    await renderApp(<FilesTab session={session()} live={live} rev={0} />, m.fetch);
    await userEvent.type(await screen.findByLabelText("Open or create file"), "docs/new.txt");
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(await screen.findByTestId("files-new-hint")).toHaveTextContent("saving creates it");
    const editor = await screen.findByLabelText("docs/new.txt");
    await userEvent.type(editor, "hello");
    await userEvent.click(screen.getByRole("button", { name: "Create file" }));
    await waitFor(() => expect(m.find("PUT", "/api/sessions/s1/files")).toHaveLength(1));
    const [put] = m.find("PUT", "/api/sessions/s1/files");
    expect(put.body).toEqual({ path: "docs/new.txt", content: "hello", expected_digest: "absent" });
    expect(put.headers["Idempotency-Key"]).toBeTruthy();
  });

  it("explains a concurrent creation instead of overwriting", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/files", { json: { items: [] } }],
      ["GET", "/api/sessions/s1/files/content", notFound],
      ["PUT", "/api/sessions/s1/files", err(409, "version_conflict", "file already exists")],
    ]);
    await renderApp(<FilesTab session={session()} live={live} rev={0} />, m.fetch);
    await userEvent.type(await screen.findByLabelText("Open or create file"), "a.txt");
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    await userEvent.type(await screen.findByLabelText("a.txt"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Create file" }));
    expect(await screen.findByText("This file was created in the meantime")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload current version" })).toBeInTheDocument();
  });

  it("does not present other errors as a missing file", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/files", { json: { items: [] } }],
      ["GET", "/api/sessions/s1/files/content", err(403, "forbidden", "credential-bearing files are not readable")],
    ]);
    await renderApp(<FilesTab session={session()} live={live} rev={0} />, m.fetch);
    await userEvent.type(await screen.findByLabelText("Open or create file"), ".env");
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(await screen.findByText(/credential-bearing files are not readable/)).toBeInTheDocument();
    expect(screen.queryByTestId("files-new-hint")).toBeNull();
    expect(screen.queryByRole("button", { name: /Create file|Save|Edit/ })).toBeNull();
  });

  it("keeps a truncated view read-only and never sends it back", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/files", { json: { items: [{ path: "big.txt", type: "file", size: 1_000_019 }] } }],
      [
        "GET",
        "/api/sessions/s1/files/content",
        { json: { path: "big.txt", encoding: "utf-8", content: "a".repeat(2000), digest: null, size: 1_000_019, truncated: true, redacted: false, editable: false, readonly_reason: "truncated" } },
      ],
    ]);
    await renderApp(<FilesTab session={session()} live={live} rev={0} />, m.fetch);
    await userEvent.click(await screen.findByRole("button", { name: /big\.txt/ }));
    expect(await screen.findByTestId("files-readonly")).toHaveTextContent("Large file: read-only preview");
    expect(screen.getByTestId("files-readonly")).toHaveTextContent("of 1,000,019 bytes");
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(m.calls.filter((c) => c.method === "PUT")).toHaveLength(0);
  });

  it("still edits a complete small file with its digest", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/files", { json: { items: [{ path: "a.txt", type: "file", size: 4 }] } }],
      ["GET", "/api/sessions/s1/files/content", { json: { path: "a.txt", encoding: "utf-8", content: "one\n", digest: "sha256:abc", truncated: false, redacted: false, editable: true, readonly_reason: null } }],
      ["PUT", "/api/sessions/s1/files", { json: { path: "a.txt", generation: 3, digest: "sha256:def" } }],
    ]);
    await renderApp(<FilesTab session={session()} live={live} rev={0} />, m.fetch);
    await userEvent.click(await screen.findByRole("button", { name: /a\.txt/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await userEvent.type(screen.getByLabelText("a.txt"), "two");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(m.find("PUT", "/api/sessions/s1/files")).toHaveLength(1));
    expect(m.find("PUT", "/api/sessions/s1/files")[0].body).toMatchObject({ expected_digest: "sha256:abc", content: "one\ntwo" });
  });
});

describe("Terminal: re-attach, rendering and uncertain input (AUD08/AUD09/AUD16)", () => {
  afterEach(() => resetTerminalViews());

  const outputRoutes = (chunks: string[]) => {
    let i = 0;
    let offset = 0;
    return (call: RecordedCall) => {
      const after = Number(call.query.get("after"));
      if (after < offset) offset = after;
      const data = chunks[i] ?? "";
      i = Math.min(i + 1, chunks.length);
      offset += data.length;
      return { json: { data, offset, closed: false } };
    };
  };

  it("re-attaches to a running terminal instead of starting another, across panel switches", async () => {
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/terminals", { json: { items: [{ terminal_id: "pty_1", created_at: null, closed: false, offset: 10 }] } }],
      ["GET", "/api/sessions/s1/terminals/pty_1/output", outputRoutes(["\x1b[?2004h$ git status --short\r\n\x1b[31m M\x1b[m a.txt\r\n$ "])],
    ]);
    const view = await renderApp(<TerminalTab sessionId="s1" pollMs={20} />, m.fetch);
    const screenEl = await screen.findByTestId("terminal-screen");
    await waitFor(() => expect(screenEl).toHaveTextContent("M a.txt"));
    expect(screenEl.textContent).not.toMatch(/\x1b|\[\?2004h|\[31m|\[m/);
    expect(within(screenEl).getByText("M", { exact: false }).className).toContain("term-fg-red");

    // Switch panels away and back: same terminal, output kept, no second shell.
    view.unmount();
    const before = m.find("GET", "/api/sessions/s1/terminals/pty_1/output").length;
    await renderApp(<TerminalTab sessionId="s1" pollMs={20} />, m.fetch);
    expect(await screen.findByTestId("terminal-screen")).toHaveTextContent("M a.txt");
    await waitFor(() => expect(m.find("GET", "/api/sessions/s1/terminals/pty_1/output").length).toBeGreaterThan(before));
    // It continues from the stored offset rather than replaying from zero.
    const resumed = m.find("GET", "/api/sessions/s1/terminals/pty_1/output").slice(before);
    expect(resumed.every((c) => Number(c.query.get("after")) > 0)).toBe(true);
    expect(m.find("POST", "/api/sessions/s1/terminals")).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
  });

  it("keeps a terminal started on this page after the panel is switched away and back", async () => {
    let items: { terminal_id: string; created_at: null; closed: boolean; offset: number }[] = [];
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/terminals", () => ({ json: { items } })],
      ["GET", "/api/sessions/s1/terminals/pty_9/output", { json: { data: "$ ", offset: 2, closed: false } }],
      [
        "POST",
        "/api/sessions/s1/terminals",
        () => {
          items = [{ terminal_id: "pty_9", created_at: null, closed: false, offset: 0 }];
          return { status: 201, json: { terminal_id: "pty_9" } };
        },
      ],
    ]);
    // One provider (and query cache) for the whole page, as in the app: only the tab unmounts.
    function Panels() {
      const [onTerminal, setOnTerminal] = useState(true);
      return (
        <>
          <button type="button" onClick={() => setOnTerminal(!onTerminal)}>
            switch panel
          </button>
          {onTerminal ? <TerminalTab sessionId="s1" pollMs={20} /> : <p>files panel</p>}
        </>
      );
    }
    await renderApp(<Panels />, m.fetch);
    await userEvent.click(await screen.findByRole("button", { name: "Start terminal" }));
    expect(await screen.findByTestId("terminal-screen")).toHaveTextContent("$");
    await waitFor(() => expect(m.find("GET", "/api/sessions/s1/terminals").length).toBeGreaterThanOrEqual(2));
    await userEvent.click(screen.getByRole("button", { name: "switch panel" }));
    expect(await screen.findByText("files panel")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "switch panel" }));
    expect(await screen.findByTestId("terminal-screen")).toHaveTextContent("$");
    await act(async () => new Promise((r) => setTimeout(r, 100)));
    expect(screen.queryByRole("button", { name: "Start terminal" })).toBeNull();
    expect(m.find("POST", "/api/sessions/s1/terminals")).toHaveLength(1);
  });

  it("closes a terminal explicitly and starts a new one only on request", async () => {
    let items = [{ terminal_id: "pty_1", created_at: null, closed: false, offset: 0 }];
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/terminals", () => ({ json: { items } })],
      ["GET", /\/api\/sessions\/s1\/terminals\/pty_\d\/output/, { json: { data: "", offset: 0, closed: false } }],
      [
        "DELETE",
        "/api/sessions/s1/terminals/pty_1",
        () => {
          items = [];
          return { json: { closed: true } };
        },
      ],
      [
        "POST",
        "/api/sessions/s1/terminals",
        () => {
          items = [{ terminal_id: "pty_2", created_at: null, closed: false, offset: 0 }];
          return { status: 201, json: { terminal_id: "pty_2" } };
        },
      ],
    ]);
    await renderApp(<TerminalTab sessionId="s1" pollMs={20} />, m.fetch);
    await userEvent.click(await screen.findByRole("button", { name: "Close" }));
    await waitFor(() => expect(m.find("DELETE", "/api/sessions/s1/terminals/pty_1")).toHaveLength(1));
    expect(m.find("DELETE", "/api/sessions/s1/terminals/pty_1")[0].headers["Idempotency-Key"]).toBeTruthy();
    await userEvent.click(await screen.findByRole("button", { name: "Start terminal" }));
    await waitFor(() => expect(m.find("POST", "/api/sessions/s1/terminals")).toHaveLength(1));
    expect(await screen.findByTestId("terminal-screen")).toBeInTheDocument();
  });

  it("surfaces an unconfirmed command and retries it only with the same key", async () => {
    let attempts = 0;
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/terminals", { json: { items: [{ terminal_id: "pty_1", created_at: null, closed: false, offset: 0 }] } }],
      ["GET", "/api/sessions/s1/terminals/pty_1/output", { json: { data: "", offset: 0, closed: false } }],
      [
        "POST",
        "/api/sessions/s1/terminals/pty_1/input",
        () => {
          attempts++;
          // The first request (and the client's automatic same-key retries) lose their response.
          return attempts <= 3 ? { status: 503, json: { error: { code: "internal_error", message: "gateway" } } } : { json: { accepted: true } };
        },
      ],
    ]);
    await renderApp(<TerminalTab sessionId="s1" pollMs={50} />, m.fetch);
    await userEvent.type(await screen.findByLabelText("Command input"), "make migrate");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    const notice = await screen.findByTestId("terminal-uncertain");
    expect(notice).toHaveTextContent("Delivery not confirmed");
    expect(notice).toHaveTextContent("make migrate");
    // A new command cannot be sent while the previous one's fate is unknown.
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await userEvent.click(within(notice).getByRole("button", { name: "Retry same command" }));
    await waitFor(() => expect(screen.queryByTestId("terminal-uncertain")).toBeNull());
    const posts = m.find("POST", "/api/sessions/s1/terminals/pty_1/input");
    expect(posts).toHaveLength(4);
    expect(new Set(posts.map((p) => p.headers["Idempotency-Key"])).size).toBe(1);
    expect(posts.every((p) => (p.body as { data: string }).data === "make migrate\n")).toBe(true);
  });
});

describe("Live changes refresh (AUD05)", () => {
  const cs: ChangeSet = {
    id: "cs1",
    session_id: "s1",
    source_turn_id: null,
    state: "ready",
    origin: "explicit",
    subject_digest: "sha256:subject",
    repository: null,
    base_sha: "b",
    head_sha: "h",
    worktree_generation: 1,
    file_count: 2,
    error: null,
    created_at: "2026-10-05T00:00:00Z",
    sealed_at: "2026-10-05T00:00:01Z",
  };

  it("re-observes on activation and on Refresh instead of showing a stale cache", async () => {
    let files = [{ path: "existing.txt", status: "??" }];
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/changes", () => ({ json: { live: true, observation: { files } } })],
      ["GET", "/api/sessions/s1/changesets", { json: { items: [] } }],
    ]);
    const first = await renderApp(<ChangesTab sessionId="s1" />, m.fetch);
    expect(await screen.findByText("existing.txt")).toBeInTheDocument();
    first.unmount();
    // A terminal creates a file: no event arrives, yet returning to the tab shows it.
    files = [...files, { path: "from-terminal.txt", status: "??" }];
    await renderApp(<ChangesTab sessionId="s1" />, m.fetch);
    expect(await screen.findByText("from-terminal.txt")).toBeInTheDocument();
    files = [{ path: "existing.txt", status: "??" }];
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.queryByText("from-terminal.txt")).toBeNull());
  });

  it("labels ChangeSet files from the patch: A for a new file, never M", async () => {
    const patch = "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+x\ndiff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-a\n+b\n";
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1/changes", { json: { live: true, observation: { files: [] } } }],
      ["GET", "/api/sessions/s1/changesets", { json: { items: [cs] } }],
      ["GET", "/api/changesets/cs1", { json: { ...cs, files: [{ path: "n.py", type: "file" }, { path: "m.py", type: "file" }] } }],
      ["GET", "/api/changesets/cs1/deliveries", { json: { items: [] } }],
      ["GET", "/api/changesets/cs1/diff", { json: { diff: patch, truncated: false } }],
    ]);
    await renderApp(<ChangesTab sessionId="s1" />, m.fetch);
    const list = await screen.findByRole("list", { name: /files/i });
    // Before the patch is known the status is neutral, not a guessed "M".
    expect(within(list).queryByText("M")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show diff" }));
    await waitFor(() => expect(within(list).getByTitle("New")).toHaveTextContent("A"));
    expect(within(list).getByTitle("Modified")).toHaveTextContent("M");
    const diff = screen.getByLabelText("Diff");
    const header = within(diff).getByText("+++ b/n.py").closest(".diff__line")!;
    expect(header.querySelectorAll(".diff__no")[1]).toHaveTextContent("");
    expect(within(diff).getByText("+x").closest(".diff__line")!.querySelectorAll(".diff__no")[1]).toHaveTextContent("1");
  });
});

describe("Review changes link (AUD11)", () => {
  it.each(["", "/overview", "/files", "/terminal", "/changes"])("points at the session changes route from %s", async (tab) => {
    const turn = { id: "t1", ordinal: 1, state: "succeeded", reason: null, retry_of_turn_id: null, error: null, outcome: null, actions: [], version: 2, started_at: "2026-10-05T00:00:00Z", finished_at: "2026-10-05T00:00:05Z" };
    const m = mockFetch([
      ...identityRoutes,
      ["GET", "/api/sessions/s1", { json: { session: session(), event_watermark: 3 } }],
      [
        "GET",
        "/api/sessions/s1/messages",
        {
          json: {
            event_watermark: 3,
            items: [
              { id: "m0", ordinal: 1, role: "user", content: [{ kind: "text", text: "Do it" }], turn_id: "t1", state: "accepted", parts: [] },
              { id: "m1", ordinal: 2, role: "assistant", content: [], turn_id: "t1", state: "completed", parts: [{ key: "p1", kind: "text", revision: 1, content: "Done", sealed: true }] },
            ],
          },
        },
      ],
      ["GET", "/api/sessions/s1/turns", { json: { items: [turn] } }],
      ["GET", "/api/sessions/s1/executor", { json: { backend: "local", leases: [], worktree: null, recovery_point: null } }],
      ["GET", "/api/sessions/s1/events", (call) => (call.headers.Accept === "text/event-stream" ? { status: 204 } : { json: { items: [], next_after: 3, event_watermark: 3 } })],
    ]);
    await renderApp(
      <Routes>
        <Route path="/sessions/:id/:tab?" element={<SessionPage />} />
      </Routes>,
      m.fetch,
      `/sessions/s1${tab}`,
    );
    const link = await screen.findByRole("link", { name: "Review changes" });
    expect(link).toHaveAttribute("href", "/sessions/s1/changes");
    await act(async () => undefined);
  });
});

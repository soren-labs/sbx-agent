import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SessionDetailPage } from "../pages/SessionDetailPage";
import { SESSIONS } from "../api/fixtures";
import { makeApi, renderRoute } from "../test/helpers";

const idle = SESSIONS.find((s) => s.phase === "idle")!;
const running = SESSIONS.find((s) => s.phase === "running")!;
const queued = SESSIONS.find((s) => s.phase === "queued")!;
const failed = SESSIONS.find((s) => s.phase === "failed")!;

function renderDetail(api = makeApi(), id = idle.id) {
  return renderRoute(`/sessions/${id}`, "/sessions/:id", <SessionDetailPage />, { api });
}

describe("SessionDetailPage", () => {
  it("renders human meta: title, status, provider/model, repo", async () => {
    renderDetail();
    expect(await screen.findByRole("heading", { level: 1, name: idle.title })).toBeInTheDocument();
    expect(screen.getByTestId("pill-idle")).toBeInTheDocument();
    expect(screen.getByTestId("provider-badge")).toHaveTextContent("codex");
    expect(screen.getAllByText("soren-labs/sbx-browser").length).toBeGreaterThan(0);
    // compact rail sections
    expect(screen.getByTestId("session-meta")).toBeInTheDocument();
  });

  it("renders the conversation with user + assistant turns and inline activity", async () => {
    renderDetail();
    const convo = await screen.findByTestId("conversation");
    expect(convo).toHaveTextContent(idle.turns[0].prompt);
    expect(convo).toHaveTextContent("GET /healthz");
    expect(convo).toHaveTextContent("You");
    expect(convo).toHaveTextContent("Assistant");
    // normalized activity inline
    expect(convo.querySelectorAll('[data-kind="command"]').length).toBeGreaterThan(0);
    expect(convo.querySelectorAll('[data-kind="file_change"]').length).toBeGreaterThan(0);
  });

  it("switches to the normalized Activity timeline", async () => {
    renderDetail();
    await screen.findByTestId("conversation");
    await userEvent.click(screen.getByTestId("tab-activity"));
    const tl = await screen.findByTestId("activity-timeline");
    expect(tl.querySelectorAll('[data-kind="status"]').length).toBeGreaterThan(0);
  });

  it("shows the Changes tab only when changes exist", async () => {
    renderDetail();
    await screen.findByTestId("conversation");
    expect(screen.getByTestId("tab-changes")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("tab-changes"));
    expect(await screen.findByTestId("changes-panel")).toHaveTextContent(
      "control/service.py",
    );
  });

  it("hides the Changes tab when the session has none", async () => {
    renderDetail(makeApi(), running.id);
    await screen.findByTestId("conversation");
    expect(screen.queryByTestId("tab-changes")).not.toBeInTheDocument();
  });

  it("shows the queued phase banner", async () => {
    renderDetail(makeApi(), queued.id);
    expect(await screen.findByTestId("phase-banner")).toHaveTextContent("Queued");
  });

  it("sends a follow-up optimistically then reconciles", async () => {
    const api = makeApi();
    renderDetail(api, idle.id);
    await screen.findByTestId("conversation");
    await userEvent.type(screen.getByTestId("followup-input"), "add docs too");
    await userEvent.click(screen.getByTestId("followup-send"));
    // optimistic turn lands immediately
    const convo = await screen.findByTestId("conversation");
    await waitFor(() => expect(convo).toHaveTextContent("add docs too"));
  });

  it("shows session failure as a product-level notice", async () => {
    renderDetail(makeApi(), failed.id);
    const notice = await screen.findByTestId("error-notice");
    expect(notice).toHaveAttribute("data-kind", "session_failed");
    expect(notice).toHaveTextContent("rejected the stored credential");
  });

  it("replaces the composer with an ended notice on closed sessions", async () => {
    renderDetail(makeApi(), SESSIONS.find((s) => s.phase === "ended")!.id);
    await screen.findByTestId("conversation");
    expect(screen.getByTestId("followup")).toHaveTextContent("has ended");
    expect(screen.queryByTestId("followup-input")).not.toBeInTheDocument();
  });

  it("stream_drops scenario surfaces the reconnect UX", async () => {
    const api = makeApi("stream_drops");
    renderDetail(api, idle.id);
    expect(await screen.findByTestId("reconnect-banner", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(await screen.findByTestId("reconnected-banner", {}, { timeout: 4000 })).toBeInTheDocument();
  });

  it("404 shows the not-found surface", async () => {
    renderDetail(makeApi(), "sess-does-not-exist");
    expect(await screen.findByTestId("error-notice")).toHaveAttribute(
      "data-kind",
      "not_found",
    );
  });
});

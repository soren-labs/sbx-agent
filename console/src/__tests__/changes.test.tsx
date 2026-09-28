import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SessionDetailPage } from "../pages/SessionDetailPage";
import { SESSIONS } from "../api/fixtures";
import { makeApi, renderRoute } from "../test/helpers";

const pending = SESSIONS.find((s) => s.id === "sess-aa00ff1122aabbcc")!;
const delivered = SESSIONS.find((s) => s.id === "sess-8f3a1c2b9d4e5f01")!;

function renderDetail(api = makeApi(), id = pending.id) {
  return renderRoute(`/sessions/${id}`, "/sessions/:id", <SessionDetailPage />, {
    api,
  });
}

async function openChangesTab() {
  await screen.findByTestId("conversation");
  await userEvent.click(screen.getByTestId("tab-changes"));
  await screen.findByTestId("changes-panel");
  // file-level diff fetched on mount
  await screen.findByTestId("changes-summary");
}

describe("Changes tab — file list + lazy diffs", () => {
  it("renders the summary and rows only when the diff resolves", async () => {
    renderDetail();
    await openChangesTab();
    expect(screen.getByTestId("changes-summary")).toHaveTextContent(
      "2 files changed",
    );
    expect(screen.getByTestId("changes-summary")).toHaveTextContent("+9");
    expect(screen.getByTestId("changes-summary")).toHaveTextContent("−1");
    expect(
      screen.getByTestId("file-console/src/pages/SessionsPage.tsx"),
    ).toHaveTextContent("modified");
    // no file diff body fetched yet — stats only
    expect(screen.queryByText("diff --git")).not.toBeInTheDocument();
  });

  it("lazy-fetches a file's diff only when its row is expanded", async () => {
    const api = makeApi();
    const spy = vi.spyOn(api, "getFileDiff");
    renderDetail(api);
    await openChangesTab();
    expect(spy).not.toHaveBeenCalled();
    const row = screen.getByTestId(
      "file-console/src/pages/SessionsPage.tsx",
    );
    await userEvent.click(row.querySelector("summary")!);
    const diff = await screen.findByText(/diff --git/, undefined, {
      timeout: 2000,
    });
    expect(diff).toBeInTheDocument();
    expect(row).toHaveTextContent("toLowerCase()");
    expect(spy).toHaveBeenCalledTimes(1);
    // base/head land in the Details rail, not the diff surface
    const meta = screen.getByTestId("session-meta");
    expect(meta).toHaveTextContent("3b6db74");
    expect(meta).toHaveTextContent("f00dbab");
    expect(meta).toHaveTextContent("#1");
  });
});

describe("Changes tab — delivery", () => {
  it("creates a pull request for ready changes", async () => {
    const api = makeApi();
    const spy = vi.spyOn(api, "deliverSession");
    renderDetail(api);
    await openChangesTab();
    expect(screen.getByTestId("deliver-card")).toHaveTextContent(
      "Changes ready",
    );
    await userEvent.click(screen.getByTestId("deliver-create"));
    await waitFor(
      () =>
        expect(screen.getByTestId("deliver-card")).toHaveTextContent(
          "Pull request ready",
        ),
      { timeout: 3000 },
    );
    const card = screen.getByTestId("deliver-card");
    expect(card.querySelector('a[href*="pull/97"]')).toBeInTheDocument();
    expect(screen.getByTestId("open-github")).toBeInTheDocument();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith(
      pending.id,
      expect.objectContaining({ title: pending.title }),
    );
  });

  it("updates the same pull request when new code arrives after delivery", async () => {
    const api = makeApi();
    renderDetail(api, delivered.id);
    await openChangesTab();
    // fixture: PR exists but a follow-up pushed a newer head
    expect(screen.getByTestId("deliver-card")).toHaveTextContent(
      "Pull request ready",
    );
    expect(screen.getByTestId("deliver-card")).toHaveTextContent(
      "New changes since this pull request",
    );
    await userEvent.click(screen.getByTestId("deliver-update"));
    await waitFor(
      () =>
        expect(screen.getByTestId("deliver-card")).toHaveTextContent(
          "Pull request updated",
        ),
      { timeout: 3000 },
    );
    // still the same PR — no revision-chain UX
    expect(screen.getByTestId("deliver-card")).toHaveTextContent("#97");
  });

  it("fails with a retry that re-delivers instead of rerunning the provider", async () => {
    const api = makeApi("deliver_fails");
    const deliver = vi.spyOn(api, "deliverSession");
    const followup = vi.spyOn(api, "sendFollowUp");
    const rerun = vi.spyOn(api, "retrySession");
    renderDetail(api);
    await openChangesTab();
    await userEvent.click(screen.getByTestId("deliver-create"));
    const failed = await screen.findByTestId("deliver-failed", undefined, {
      timeout: 3000,
    });
    expect(failed).toHaveTextContent("Delivery failed");
    expect(failed).toHaveTextContent("the remote rejected the push");
    await userEvent.click(screen.getByTestId("deliver-retry"));
    await waitFor(() => expect(deliver).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("deliver-failed")).toBeInTheDocument();
    expect(followup).not.toHaveBeenCalled();
    expect(rerun).not.toHaveBeenCalled();
  });

  it("offers Connect GitHub when delivery needs it — changes stay local", async () => {
    const api = makeApi("github_required");
    renderDetail(api);
    await openChangesTab();
    const card = screen.getByTestId("deliver-card");
    await waitFor(() =>
      expect(card).toHaveTextContent(
        "Changes are ready locally. Connect GitHub to create a pull request.",
      ),
    );
    expect(screen.getByTestId("connect-github")).toBeInTheDocument();
    // the local result is still there — file list intact behind the card
    expect(screen.getByTestId("file-list")).toBeInTheDocument();
  });
});

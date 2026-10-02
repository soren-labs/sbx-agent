import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Changes } from "../prototype/Panels";
import { ApiProvider } from "../state/api";
import { HttpSessionApi } from "../api/http";
import { SESSIONS } from "../api/fixtures";
it("pins lazy diffs to the displayed revision and retries a failed file request", async () => {
  const client = new HttpSessionApi();
  vi.spyOn(client, "listChangesDiff").mockResolvedValue({
    n: 3,
    filesChanged: 1,
    additions: 1,
    deletions: 0,
    files: [{ path: "a.ts", status: "added", additions: 1, deletions: 0 }],
  });
  const diff = vi
    .spyOn(client, "getFileDiff")
    .mockRejectedValueOnce(new Error("Offline"))
    .mockResolvedValue({
      path: "a.ts",
      status: "added",
      additions: 1,
      deletions: 0,
      diff: "+hello",
    });
  render(
    <ApiProvider client={client}>
      <Changes
        session={{ ...SESSIONS[0], hasChanges: true }}
        onDeliver={() => {}}
        onReview={() => {}}
        busy={false}
      />
    </ApiProvider>,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Could not load this file",
  );
  fireEvent.click(screen.getByRole("button", { name: "Retry changes" }));
  await waitFor(() =>
    expect(screen.getByLabelText("Diff for a.ts")).toHaveTextContent("+hello"),
  );
  expect(diff).toHaveBeenLastCalledWith(SESSIONS[0].id, "a.ts", 3);
});
describe("delivery wire", () => {
  it("includes the exact revision and draft transition", async () => {
    const fetch = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            session: {
              id: "s",
              status: "finished",
              phase: "finished",
              delivery: {
                status: "delivered",
                pull_request: {
                  number: 1,
                  url: "https://github.com/o/r/pull/1",
                  draft: false,
                  state: "open",
                },
              },
            },
            revision: { n: 3 },
          }),
          { status: 200 },
        ),
      );
    const result = await new HttpSessionApi().deliverSession("s", {
      n: 3,
      title: "Review",
      draft: false,
      target: "test-base",
    });
    expect(JSON.parse(fetch.mock.calls[0][1]!.body as string)).toMatchObject({
      n: 3,
      pull_request: { draft: false, target: "test-base" },
    });
    expect(result.session.delivery?.prState).toBe("open");
    fetch.mockRestore();
  });
});
it("restores a merge result from the actual delivery.merge projection", async () => {
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          revisions: [
            {
              n: 3,
              delivery: { status: "delivered", merge: { merged: true } },
            },
          ],
        }),
        { status: 200 },
      ),
    );
  expect((await new HttpSessionApi().listChanges("s"))[0].merged).toBe(true);
  fetch.mockRestore();
});
it("shows loading rather than a false empty changes state while the snapshot is pending", async()=>{
 const client=new HttpSessionApi();vi.spyOn(client,"listChangesDiff").mockImplementation(()=>new Promise(()=>{}));
 render(<ApiProvider client={client}><Changes session={SESSIONS[0]} onDeliver={()=>{}} onReview={()=>{}} busy={false}/></ApiProvider>);
 expect(screen.getByRole("status")).toHaveTextContent("Loading changes");expect(screen.queryByText("No file changes yet.")).not.toBeInTheDocument();
});

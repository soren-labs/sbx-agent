import { describe, expect, it } from "vitest";
import { computeSetup } from "../features/setup/checklist";
import { ME, connection } from "../test/helpers";

describe("setup checklist", () => {
  it("is complete with a verified account + Modal + GitHub + Zen and no Codex", () => {
    const s = computeSetup(ME, [connection({ kind: "modal" }), connection({ kind: "github" }), connection({ kind: "opencode_zen" })]);
    expect(s.complete).toBe(true);
    expect(s.next).toBeNull();
    const codex = s.items.find((i) => i.id === "codex")!;
    expect(codex).toMatchObject({ required: false, status: "missing" });
  });

  it("requires every minimum item and points at the first gap", () => {
    const s = computeSetup(ME, [connection({ kind: "modal" }), connection({ kind: "opencode_zen" })]);
    expect(s.complete).toBe(false);
    expect(s.next?.id).toBe("github");
    expect(computeSetup(null, []).next?.id).toBe("account");
  });

  it("renders server health instead of deciding readiness", () => {
    const s = computeSetup(ME, [
      connection({ kind: "modal", health: "verifying" }),
      connection({ kind: "github", health: "reauth_required" }),
      connection({ kind: "opencode_zen", state: "revoked" }),
    ]);
    const by = Object.fromEntries(s.items.map((i) => [i.id, i.status]));
    expect(by).toMatchObject({ modal: "pending", github: "attention", opencode_zen: "missing" });
    expect(s.complete).toBe(false);
  });

  it("treats Codex health as irrelevant to completion", () => {
    const base = [connection({ kind: "modal" }), connection({ kind: "github" }), connection({ kind: "opencode_zen" })];
    const s = computeSetup(ME, [...base, connection({ kind: "codex", health: "degraded" })]);
    expect(s.complete).toBe(true);
  });
});

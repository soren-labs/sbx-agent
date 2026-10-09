import { describe, expect, it } from "vitest";
import { computeSetup } from "../features/setup/checklist";
import { ME, connection } from "../test/helpers";

const READY = [connection({ kind: "modal" }), connection({ kind: "github" }), connection({ kind: "inference_api" })];

describe("setup checklist", () => {
  it("is complete with a verified account, an inference API key, Modal and GitHub", () => {
    const s = computeSetup(ME, READY);
    expect(s.complete).toBe(true);
    expect(s.next).toBeNull();
    expect(s.items.map((i) => i.id)).toEqual(["account", "inference_api", "modal", "github"]);
  });

  it("requires every minimum item and points at the first gap", () => {
    const s = computeSetup(ME, [connection({ kind: "modal" }), connection({ kind: "inference_api" })]);
    expect(s.complete).toBe(false);
    expect(s.next?.id).toBe("github");
    expect(computeSetup(ME, [connection({ kind: "modal" })]).next?.id).toBe("inference_api");
    expect(computeSetup(null, []).next?.id).toBe("account");
  });

  it("renders server health instead of deciding readiness", () => {
    const s = computeSetup(ME, [
      connection({ kind: "modal", health: "verifying" }),
      connection({ kind: "github", health: "reauth_required" }),
      connection({ kind: "inference_api", state: "revoked" }),
    ]);
    const by = Object.fromEntries(s.items.map((i) => [i.id, i.status]));
    expect(by).toMatchObject({ modal: "pending", github: "attention", inference_api: "missing" });
    expect(s.complete).toBe(false);
  });

  it("does not count retired vendor-specific connections as inference", () => {
    const s = computeSetup(ME, [
      connection({ kind: "modal" }),
      connection({ kind: "github" }),
      connection({ kind: "opencode_zen", legacy: true }),
      connection({ kind: "codex", legacy: true }),
    ]);
    expect(s.complete).toBe(false);
    expect(s.next?.id).toBe("inference_api");
  });
});

import { describe, expect, it } from "vitest";
import { mergeEvents } from "../state/unified";

describe("mergeEvents (committed-replay store)", () => {
  it("dedupes by seq and never lowers the watermark", () => {
    let tail = mergeEvents(undefined, [
      { seq: 1, type: "a" } as never,
      { seq: 2, type: "b" } as never,
    ], 2);
    expect(tail.items.map((e) => e.seq)).toEqual([1, 2]);
    expect(tail.watermark).toBe(2);

    // overlapping re-poll: seq 2 replayed, seq 3 new
    tail = mergeEvents(tail, [
      { seq: 2, type: "b" } as never,
      { seq: 3, type: "c" } as never,
    ], 3);
    expect(tail.items.map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(tail.watermark).toBe(3);

    // stale snapshot: watermark can only go forward
    tail = mergeEvents(tail, [], 1);
    expect(tail.watermark).toBe(3);
  });

  it("keeps sort order when batches arrive out of order", () => {
    const tail = mergeEvents(
      mergeEvents(undefined, [{ seq: 4, type: "d" } as never], 4),
      [{ seq: 3, type: "c" } as never],
      5,
    );
    expect(tail.items.map((e) => e.seq)).toEqual([3, 4]);
  });
});

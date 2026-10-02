import { describe, expect, it, vi } from "vitest";
import { readSessionCache, writeSessionCache } from "../prototype/session-cache";
import type { Session } from "../api/types";
const session = {id:"s",turns:[],title:"history"} as unknown as Session;
describe("browser transcript cache",()=>{
  it("scopes history to a connection and expires old data",()=>{
    sessionStorage.clear();writeSessionCache("connection-a",session);
    expect(readSessionCache("connection-a","s")).toEqual(session);
    expect(readSessionCache("connection-b","s")).toBeNull();
    vi.spyOn(Date,"now").mockReturnValue(Date.now()+31*60_000);
    expect(readSessionCache("connection-a","s")).toBeNull();vi.restoreAllMocks();
  });
  it("tolerates corrupt data and storage failure",()=>{
    sessionStorage.setItem("sbx.history.v1.a.s","{broken");expect(readSessionCache("a","s")).toBeNull();
    vi.spyOn(Storage.prototype,"setItem").mockImplementation(()=>{throw new Error("quota");});
    expect(()=>writeSessionCache("a",session)).not.toThrow();vi.restoreAllMocks();
  });
});

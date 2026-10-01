import { describe, expect, it } from "vitest";
import { mergeActivity, mergeSession, mergeTurn } from "../prototype/session-state";
import { SESSIONS } from "../api/fixtures";
import type { ActivityItem } from "../api/types";
const item: ActivityItem = {id:"item-1",seq:1,ts:"2026-10-01T00:00:00Z",turnId:"turn-1",kind:"command",command:"ls",status:"running"};
describe("stream and snapshot convergence",()=>{
  it("keeps first order/time while completing a command, without duplicates",()=>{
    const rows=mergeActivity([item,{...item,id:"item-2",seq:2}], [{...item,seq:10,ts:"2026-10-01T01:00:00Z",status:"finished",output:"README"}]);
    expect(rows).toHaveLength(2);expect(rows[0]).toMatchObject({seq:1,ts:item.ts,output:"README",status:"finished"});
  });
  it("preserves evidence and accepted prompts across activity-free detail responses",()=>{
    const current=structuredClone(SESSIONS[0]);current.turns[0].activity=[item];
    const snapshot=structuredClone(current);snapshot.turns[0].activity=[];snapshot.turns[0].status="finished";
    const next=mergeSession(current,snapshot);
    expect(next.turns[0].activity).toEqual([item]);expect(next.turns[0].status).toBe("finished");
    expect(mergeTurn(next.turns[0],{...snapshot.turns[0],prompt:"",createdAt:""}).prompt).toBe(current.turns[0].prompt);
  });
  it("does not regress session phase from an older detail request",()=>{
    const current=structuredClone(SESSIONS[0]);current.phase="idle";current.updatedAt="2026-10-01T01:00:00Z";
    const older={...current,phase:"running" as const,updatedAt:"2026-10-01T00:00:00Z"};
    expect(mergeSession(current,older).phase).toBe("idle");
  });
});

import { vi, afterEach } from "vitest";
import { HttpSessionApi } from "../api/http";
afterEach(()=>{vi.unstubAllGlobals();vi.useRealTimers();});
it("resumes a real-shaped stream at the last ID after transport EOF", async()=>{
  vi.useFakeTimers();
  const encoder=new TextEncoder();
  const frame='id: 12\ndata: {"type":"item.completed","n":1,"item":{"id":"cmd","type":"command_execution","command":"ls","status":"completed"}}\n\n';
  const fetchMock=vi.fn().mockResolvedValueOnce(new Response(new ReadableStream({start(c){c.enqueue(encoder.encode(frame));c.close();}}),{status:200})).mockResolvedValueOnce(new Response(new ReadableStream({start(){}}),{status:200}));
  vi.stubGlobal("fetch",fetchMock);
  const activity=vi.fn();const stop=new HttpSessionApi().subscribe("session",{onActivity:activity});
  await vi.advanceTimersByTimeAsync(1000);
  expect(fetchMock.mock.calls[1][1].headers["Last-Event-ID"]).toBe("12");
  expect(activity).toHaveBeenCalledWith(expect.objectContaining({kind:"command",command:"ls",turnId:"turn-1"}));
  stop();
});
it("does not repeatedly retry an unauthorized event stream",async()=>{
  vi.useFakeTimers();const fetchMock=vi.fn().mockResolvedValue(new Response(JSON.stringify({error:{code:"unauthorized"}}),{status:401}));vi.stubGlobal("fetch",fetchMock);
  const error=vi.fn();const stop=new HttpSessionApi().subscribe("session",{onError:error});
  await vi.advanceTimersByTimeAsync(30000);expect(fetchMock).toHaveBeenCalledTimes(1);expect(error).toHaveBeenCalled();stop();
});

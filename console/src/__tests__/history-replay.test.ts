import { expect, it, vi } from "vitest";
import { HttpSessionApi } from "../api/http";
it("coalesces partial durable history by item id",async()=>{
  vi.stubGlobal("fetch",vi.fn().mockResolvedValue(new Response(JSON.stringify({runs:[{n:1,status:"finished",prompt:"one"}],events:[
    {id:1,event:{type:"item.started",n:1,item:{id:"m",type:"agent_message",text:"He"}}},
    {id:2,event:{type:"item.updated",n:1,item:{id:"m",type:"agent_message",text:"Hello"}}},
    {id:3,event:{type:"item.completed",n:1,item:{id:"m",type:"agent_message",text:"Hello world"}}}
  ],has_more:false}),{status:200})));
  const page=await new HttpSessionApi().getHistory("s",2);
  expect(page.turns[0].activity).toHaveLength(1);
  expect(page.turns[0].activity[0]).toMatchObject({text:"Hello world",status:"finished"});
  vi.unstubAllGlobals();
});

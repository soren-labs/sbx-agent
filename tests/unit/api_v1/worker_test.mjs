// Node-side checks for deploy/sbx-edge/worker.js /v1 passthrough behavior.
// Run by test_worker.py via `node <this file>`; exits non-zero on failure.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";

const workerPath = process.argv[2];
const worker = (await import(pathToFileURL(workerPath).href)).default;

const env = {
  SBX_CONTROL_URL: "https://control.internal",
  WEB_ORIGIN: "https://web.internal",
  SBX_BASIC_USER: "sbx",
  SBX_BASIC_PASS: "sbx",
};

let captured = null;
globalThis.fetch = async (dest, init) => {
  captured = { dest, init };
  return new Response("upstream", {
    status: 200,
    headers: { "www-authenticate": "Basic realm=x", "x-up": "1" },
  });
};

// --- /v1 passes client Authorization through, never injects Basic ---
let resp = await worker.fetch(
  new Request("https://sbx.sorenforge.com/v1/agents", {
    method: "POST",
    headers: { authorization: "Bearer sbx_clientkey", "content-type": "application/json" },
    body: "{}",
  }),
  env,
);
assert.equal(resp.status, 200);
assert.equal(captured.dest, "https://control.internal/v1/agents");
assert.equal(captured.init.headers.get("authorization"), "Bearer sbx_clientkey");
assert.equal(captured.init.method, "POST");

// --- /v1 without client auth: no Authorization synthesized ---
await worker.fetch(new Request("https://sbx.sorenforge.com/v1/me"), env);
assert.equal(captured.init.headers.get("authorization"), null);

// --- /v1 response keeps upstream WWW-Authenticate (Bearer challenges) ---
resp = await worker.fetch(new Request("https://sbx.sorenforge.com/v1/me"), env);
assert.equal(resp.headers.get("www-authenticate"), "Basic realm=x");

// --- /v1 works without Basic secrets configured ---
resp = await worker.fetch(new Request("https://sbx.sorenforge.com/v1/me"), {
  SBX_CONTROL_URL: env.SBX_CONTROL_URL,
});
assert.equal(resp.status, 200);

// --- /api still injects Basic and strips WWW-Authenticate ---
resp = await worker.fetch(new Request("https://sbx.sorenforge.com/api/sessions"), env);
assert.match(captured.init.headers.get("authorization") || "", /^Basic /);
assert.equal(resp.headers.get("www-authenticate"), null);

// --- Last-Event-ID survives the hop for SSE resume ---
await worker.fetch(
  new Request("https://sbx.sorenforge.com/v1/agents/a/runs/run-1/stream", {
    headers: { "last-event-id": "7", authorization: "Bearer sbx_k" },
  }),
  env,
);
assert.equal(captured.init.headers.get("last-event-id"), "7");

// --- non-API paths still go to the web origin ---
await worker.fetch(new Request("https://sbx.sorenforge.com/index.html"), env);
assert.equal(captured.dest, "https://web.internal/index.html");

console.log("worker /v1 passthrough checks passed");

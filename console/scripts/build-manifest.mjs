// SOR-266: post-build provenance manifest for the production cutover.
//
// Runs after `vite build` and writes dist/build-manifest.json recording
// the git SHA the bundle was built from plus a sha256 over every emitted
// file. Deploy + the SOR-270 gate read it to prove the served bundle is
// the same-SHA console/dist; no credentials or tokens are ever recorded.

import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const dist = fileURLToPath(new URL("../dist/", import.meta.url));

const files = [];
function walk(dir) {
  for (const name of readdirSync(dir).sort()) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p);
    else files.push(p);
  }
}
walk(dist);

const entries = files.map((p) => ({
  path: relative(dist, p).split("\\").join("/"),
  sha256: createHash("sha256").update(readFileSync(p)).digest("hex"),
  bytes: statSync(p).size,
}));

// Vite emits exactly one entry bundle as assets/index-<hash>.js — the
// "primary asset" whose hash the deployment manifest pins.
const primary =
  entries.find((e) => /^assets\/index-[^/]+\.js$/.test(e.path)) ??
  entries.find((e) => /^assets\/[^/]+\.js$/.test(e.path)) ??
  null;

const manifest = {
  schema: "sbx-console-build/1",
  frontend_source: "console/",
  git_sha: process.env.VITE_BUILD_SHA || process.env.GIT_SHA || null,
  api_mode: process.env.VITE_API_MODE || null,
  built_at: new Date().toISOString(),
  primary_asset: primary,
  files: entries,
};

writeFileSync(
  join(dist, "build-manifest.json"),
  JSON.stringify(manifest, null, 2) + "\n",
);
console.log(
  `console build-manifest: ${entries.length} files, ` +
    `primary ${primary ? `${primary.path} ${primary.sha256.slice(0, 12)}…` : "none"} ` +
    `@ ${manifest.git_sha ?? "unknown-sha"}`,
);

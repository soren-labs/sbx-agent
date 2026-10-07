import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const pkg = JSON.parse(readFileSync(resolve(root, 'package.json'), 'utf8'));
const gitSha = process.env.SBX_BUILD_SHA || process.env.GITHUB_SHA || 'unknown';
const out = resolve(root, 'dist', 'build.json');

mkdirSync(dirname(out), { recursive: true });
writeFileSync(
  out,
  `${JSON.stringify({ product: 'sbx-agent', version: pkg.version, git_sha: gitSha }, null, 2)}\n`,
);

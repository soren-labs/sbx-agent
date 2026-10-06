// Copy the generated unified OpenAPI document (docs/specs/unified/openapi.yaml)
// to where starlight-openapi reads it, and publish a JSON copy at /openapi.json.
// The spec itself is produced by `make openapi` and drift-checked by
// tests/unit/test_openapi_drift.py; nothing is rewritten here.
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import yaml from 'js-yaml';

const here = dirname(fileURLToPath(import.meta.url));
const siteRoot = resolve(here, '..');
const source = resolve(siteRoot, '../docs/specs/unified/openapi.yaml');
const target = resolve(siteRoot, '.generated/openapi.yaml');
const jsonTarget = resolve(siteRoot, 'public/openapi.json');

mkdirSync(dirname(target), { recursive: true });
copyFileSync(source, target);
console.log(`prepare-openapi: copied ${source} -> ${target}`);

const doc = yaml.load(readFileSync(source, 'utf8'));
mkdirSync(dirname(jsonTarget), { recursive: true });
writeFileSync(jsonTarget, `${JSON.stringify(doc, null, 2)}\n`);
console.log(`prepare-openapi: wrote ${jsonTarget}`);

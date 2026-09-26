// Derive the docs copy of the public /v1 OpenAPI contract.
//
// Preferred source: the spec the control plane actually serves — generated
// from FastAPI/Pydantic models by control.api_v1.openapi.build_v1_openapi.
// When python/uv is unavailable (docs-only tooling), we fall back to the
// frozen contract docs/contracts/api-v1.yaml; tests/unit/api_v1/
// test_openapi_parity.py keeps the two identical.
//
// The copy only swaps the few internal (Chinese / ticket-tagged) prose fields
// for reader-facing English; paths, schemas and enums are untouched.
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import yaml from 'js-yaml';

const here = dirname(fileURLToPath(import.meta.url));
const siteRoot = resolve(here, '..');
const repoRoot = resolve(siteRoot, '..');
const contract = resolve(repoRoot, 'docs/contracts/api-v1.yaml');
const target = resolve(siteRoot, '.generated/api-v1.yaml');

const RUNTIME_SNIPPET = [
	'import json',
	'from control.api_v1 import router',
	'from control.api_v1.openapi import build_v1_openapi',
	'print(json.dumps(build_v1_openapi(router)))',
].join('\n');

function loadSpec() {
	try {
		const out = execFileSync('uv', ['run', '--quiet', 'python', '-c', RUNTIME_SNIPPET], {
			cwd: repoRoot,
			encoding: 'utf8',
			stdio: ['ignore', 'pipe', 'pipe'],
		});
		console.log('prepare-openapi: using runtime-generated spec (control.api_v1.openapi)');
		return JSON.parse(out);
	} catch (err) {
		console.warn(
			`prepare-openapi: runtime spec unavailable (${err.message.split('\n')[0]}); ` +
				'falling back to docs/contracts/api-v1.yaml',
		);
		return yaml.load(readFileSync(contract, 'utf8'));
	}
}

const doc = loadSpec();

doc.info.title = 'sbx-browser REST API';
doc.info.description = [
	'The public, versioned API of an sbx-browser control plane. Its shape follows the',
	'Cursor Cloud Agents API: an **agent** is one isolated sandbox running an official',
	'provider CLI, and a **run** is one turn of work on that agent. Creating an agent',
	'immediately queues its first run; follow-ups are new runs on the same agent.',
	'',
	'**Authentication** — send `Authorization: Bearer sbx_<key>`. The control plane stores',
	'only `sha256(key)`. Keys carry the `agents` scope (default) and optionally `admin`,',
	'which is required for accounts, API keys, account verification and GitHub App',
	'administration.',
	'',
	'**Errors** — every non-2xx response has the body `{"error": {"code", "message",',
	'"retry_after"?}}`. A failed run additionally carries a structured `error`',
	'(`code`, `source`, `message`, `retryable`, `retry_after?`) on its durable record,',
	'so callers can diagnose failures without parsing the event stream.',
	'',
	'**Streaming** — `GET /v1/agents/{id}/runs/{runId}/stream` is Server-Sent Events:',
	'`id` is the 1-based line number in the sandbox event log, `event` is the event type',
	'and `data` is the JSON event. A `: keepalive` comment is sent every 15 s and',
	'`Last-Event-ID` resumes after a disconnect.',
].join('\n');

// Every reader runs their own control plane; don't point samples at one deployment.
doc.servers = [{ url: 'https://sbx.example.com', description: 'Your control plane (printed by `sbx deploy`)' }];

const streamOps = doc.paths?.['/v1/agents/{agent_id}/runs/{run_id}/stream'] ??
	doc.paths?.['/v1/agents/{id}/runs/{runId}/stream'];
for (const param of streamOps?.get?.parameters ?? []) {
	if (param.name === 'Last-Event-ID') {
		param.description = 'Last event id received; the stream resumes from the next line.';
	}
}
const runError = doc.components?.schemas?.RunError;
if (runError) {
	runError.description = [
		'Structured reason a run failed. `code` is a canonical run error code; `source`',
		'tells which layer failed (`provider`, `runtime`, `control`, `telemetry`);',
		'`message` is redacted diagnostic text; `retryable` says whether retrying later',
		'(or on another account/model) may succeed; `retry_after` is the provider retry',
		'hint in seconds when known. `event_parse_error` is never downgraded to success.',
	].join('\n');
}

// Internal ticket tags ("(SOR-83)", "SOR-179: …") mean nothing to API readers.
const TICKET_PAREN =
	/\s*\((?:SOR-\d+(?:\/[A-Za-z0-9-]+)?)(?:\s*[,/]\s*SOR-\d+(?:\/[A-Za-z0-9-]+)?)*\)/g;
const TICKET_LEAD = /\bSOR-\d+(?:\/[A-Za-z0-9-]+)?(?:\s*\([^)]*\))?:?\s+/g;

function clean(text) {
	const out = text
		.replace(TICKET_PAREN, '')
		.replace(/predating SOR-\d+/g, 'created before this field existed')
		.replace(TICKET_LEAD, '');
	return out.charAt(0).toUpperCase() + out.slice(1);
}

function walk(node) {
	if (Array.isArray(node)) return node.forEach(walk);
	if (!node || typeof node !== 'object') return;
	for (const [key, value] of Object.entries(node)) {
		if ((key === 'summary' || key === 'description') && typeof value === 'string') {
			node[key] = clean(value);
		} else {
			walk(value);
		}
	}
}
walk(doc.paths);
walk(doc.components);

// The frozen contract has one dangling ref (`#/components/schemas/AgentId`
// under `…/runs/{runId}` where only `components/parameters/AgentId` exists).
// Repair it in the docs copy: substitute the parameter's own schema. The
// runtime-generated spec does not have this defect.
(function repairDanglingSchemaRefs(node) {
	if (Array.isArray(node)) return node.forEach(repairDanglingSchemaRefs);
	if (!node || typeof node !== 'object') return;
	const ref = node.$ref;
	if (typeof ref === 'string' && ref.startsWith('#/components/schemas/')) {
		const name = ref.slice('#/components/schemas/'.length);
		if (!doc.components?.schemas?.[name] && doc.components?.parameters?.[name]?.schema) {
			delete node.$ref;
			Object.assign(node, doc.components.parameters[name].schema);
		}
	}
	for (const value of Object.values(node)) repairDanglingSchemaRefs(value);
})(doc.paths ?? {});

mkdirSync(dirname(target), { recursive: true });
writeFileSync(target, yaml.dump(doc, { lineWidth: 100, noRefs: true }));
console.log(`prepare-openapi: wrote ${target}`);

// Stable machine-readable entry point at /openapi.json on the built site.
// Same content the control plane serves at /v1/openapi.json (parity is
// enforced by tests/unit/api_v1/test_openapi_parity.py).
const jsonTarget = resolve(siteRoot, 'public/openapi.json');
writeFileSync(jsonTarget, JSON.stringify(doc, null, 2) + '\n');
console.log(`prepare-openapi: wrote ${jsonTarget}`);

// Derive the docs copy of the public /v1 OpenAPI contract.
//
// docs/contracts/api-v1.yaml is frozen and stays the single source of truth.
// The copy only swaps the few internal (Chinese / ticket-tagged) prose fields
// for reader-facing English; paths, schemas and enums are untouched.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import yaml from 'js-yaml';

const here = dirname(fileURLToPath(import.meta.url));
const source = resolve(here, '../../docs/contracts/api-v1.yaml');
const target = resolve(here, '../.generated/api-v1.yaml');

const doc = yaml.load(readFileSync(source, 'utf8'));

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

const streamParams = doc.paths['/v1/agents/{id}/runs/{runId}/stream'].get.parameters;
for (const param of streamParams) {
	if (param.name === 'Last-Event-ID') {
		param.description = 'Last event id received; the stream resumes from the next line.';
	}
}
doc.components.schemas.RunError.description = [
	'Structured reason a run failed. `code` is a canonical run error code; `source`',
	'tells which layer failed (`provider`, `runtime`, `control`, `telemetry`);',
	'`message` is redacted diagnostic text; `retryable` says whether retrying later',
	'(or on another account/model) may succeed; `retry_after` is the provider retry',
	'hint in seconds when known. `event_parse_error` is never downgraded to success.',
].join('\n');

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

mkdirSync(dirname(target), { recursive: true });
writeFileSync(target, yaml.dump(doc, { lineWidth: 100, noRefs: true }));
console.log(`prepare-openapi: wrote ${target}`);

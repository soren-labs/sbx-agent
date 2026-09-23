// Copies the screenshots taken by the console Playwright suite (`make test-e2e`)
// into the docs site, so the console guide always shows the current UI.
// Usage: node docs-site/scripts/sync-console-screenshots.mjs  (or `make docs-screenshots`)
import { copyFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const from = join(root, 'tests', 'e2e', 'artifacts');
const to = join(root, 'docs-site', 'src', 'assets', 'console');

// e2e artifact → docs asset name (referenced from guides/console.mdx).
const MAP = {
	'console_01_connect.png': 'connect.png',
	'console_03_new_agent.png': 'new-agent.png',
	'console_04_conversation.png': 'conversation.png',
	'console_06_run_error.png': 'run-error.png',
	'console_07_workspace.png': 'workspace.png',
	'console_08_artifact.png': 'artifact.png',
	'console_09_workflow.png': 'workflow.png',
	'console_10_agents.png': 'agents.png',
	'console_11_capacity.png': 'capacity.png',
	'console_12_accounts.png': 'accounts.png',
	'console_13_key_created.png': 'key-created.png',
	'console_14_zh_light.png': 'zh-light.png',
};

const missing = Object.keys(MAP).filter((name) => !existsSync(join(from, name)));
if (missing.length) {
	console.error(`missing e2e screenshots (run \`make test-e2e\` first): ${missing.join(', ')}`);
	process.exit(1);
}
mkdirSync(to, { recursive: true });
for (const [src, dest] of Object.entries(MAP)) {
	copyFileSync(join(from, src), join(to, dest));
	console.log(`${src} -> docs-site/src/assets/console/${dest}`);
}

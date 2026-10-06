// Build the machine-readable bundles served at /llms-full.txt and /version.json
// from the canonical pages. /llms.txt is maintained by hand in public/.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const site = join(here, '..');
const content = join(site, 'src', 'content', 'docs');
const publicDir = join(site, 'public');
mkdirSync(publicDir, { recursive: true });

const sources = [
  'getting-started/introduction.md',
  'getting-started/quick-start.md',
  'concepts/index.md',
  'guides/connections.md',
  'guides/sessions-and-turns.md',
  'guides/changes-and-delivery.md',
  'guides/child-sessions.md',
  'guides/console.md',
  'api/overview.md',
  'api/events.md',
  'api/errors.md',
  'sdk/python.md',
  'reference/cli.md',
  'reference/providers.md',
  'self-hosting/configuration.md',
  'self-hosting/security.md',
  'troubleshooting/index.md',
];

function titleOf(text, fallback) {
  const front = text.match(/^---\n([\s\S]*?)\n---\n/);
  if (!front) return fallback;
  const title = front[1].match(/^title:\s*(.+)$/m);
  return title ? title[1].trim().replace(/^['"]|['"]$/g, '') : fallback;
}

function clean(text) {
  text = text.replace(/^---\n[\s\S]*?\n---\n/, '');
  text = text.replace(/^import .*;\s*$/gm, '');
  return text.replace(/\n{3,}/g, '\n\n').trim();
}

const sections = sources.map((rel) => {
  const raw = readFileSync(join(content, rel), 'utf8');
  const route = rel.replace(/(?:\/index)?\.(?:md|mdx)$/, '');
  return `\n\n# ${titleOf(raw, rel)}\n\nSource: /${route}/\n\n${clean(raw)}`;
});

const header =
  '# SBX documentation bundle\n\n' +
  'Generated from the canonical documentation pages. For exact request and response schemas ' +
  'use /openapi.json; for the compact index use /llms.txt.';
writeFileSync(join(publicDir, 'llms-full.txt'), `${header}${sections.join('')}\n`);

const pkg = JSON.parse(readFileSync(join(site, 'package.json'), 'utf8'));
const docsUrl = (process.env.DOCS_SITE_URL || '').replace(/\/$/, '');
const version = {
  product: 'sbx',
  version: pkg.version,
  api: '/api',
  docs_url: docsUrl || null,
  openapi_url: docsUrl ? `${docsUrl}/openapi.json` : '/openapi.json',
  llms_url: docsUrl ? `${docsUrl}/llms.txt` : '/llms.txt',
  llms_full_url: docsUrl ? `${docsUrl}/llms-full.txt` : '/llms-full.txt',
};
writeFileSync(join(publicDir, 'version.json'), `${JSON.stringify(version, null, 2)}\n`);
console.log(`generated llms-full.txt from ${sources.length} canonical pages`);
console.log(`generated version.json for ${pkg.version}`);

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
  'getting-started/quick-start.mdx',
  'concepts/index.md',
  'guides/tasks.md',
  'guides/repositories.md',
  'guides/streaming.md',
  'guides/recovery.md',
  'guides/examples.md',
  'api/overview.md',
  'api/authentication.md',
  'api/task-lifecycle.md',
  'api/idempotency.md',
  'api/pagination.md',
  'sdk/python/quickstart.md',
  'sdk/python/tasks.md',
  'sdk/python/streaming.md',
  'sdk/python/delivery.md',
  'sdk/python/errors.md',
  'integrations/providers.md',
  'integrations/github.md',
  'integrations/modal.md',
  'self-hosting/deploy.md',
  'self-hosting/security.md',
  'self-hosting/state-and-backup.md',
  'self-hosting/custom-domain.md',
  'troubleshooting/index.md',
  'agents/guide.md',
];

function titleOf(text, fallback) {
  const front = text.match(/^---\n([\s\S]*?)\n---\n/);
  if (!front) return fallback;
  const title = front[1].match(/^title:\s*(.+)$/m);
  return title ? title[1].trim().replace(/^['\"]|['\"]$/g, '') : fallback;
}

function clean(text) {
  text = text.replace(/^---\n[\s\S]*?\n---\n/, '');
  text = text.replace(/^import .*;\s*$/gm, '');
  // Strip Starlight component tags while keeping their Markdown/text body.
  text = text.replace(/^<\/?(?:Tabs|TabItem|Steps|Aside|Card|CardGrid|LinkCard)[^>]*>\s*$/gm, '');
  return text.replace(/\n{3,}/g, '\n\n').trim();
}

const sections = sources.map((rel) => {
  const raw = readFileSync(join(content, rel), 'utf8');
  const body = clean(raw);
  const title = titleOf(raw, rel);
  return `\n\n# ${title}\n\nSource: /${rel.replace(/(?:index)?\.(?:md|mdx)$/, '').replace(/\/$/, '')}/\n\n${body}`;
});

const header = `# sbx-browser — public documentation bundle\n\n` +
  `Generated from canonical public documentation pages. For exact request/response schemas use /openapi.json; for the compact index use /llms.txt.`;
writeFileSync(join(publicDir, 'llms-full.txt'), `${header}${sections.join('')}\n`);
mkdirSync(join(publicDir, 'zh-cn'), { recursive: true });
writeFileSync(join(publicDir, 'zh-cn', 'llms-full.txt'), `${header}${sections.join('')}\n`);

const pkg = JSON.parse(readFileSync(join(site, 'package.json'), 'utf8'));
const docsUrl = (process.env.DOCS_SITE_URL || '').replace(/\/$/, '');
const version = {
  product: 'sbx-browser',
  version: pkg.version,
  api_version: 'v1',
  channel: 'latest',
  docs_url: docsUrl || null,
  openapi_url: docsUrl ? `${docsUrl}/openapi.json` : '/openapi.json',
  llms_url: docsUrl ? `${docsUrl}/llms.txt` : '/llms.txt',
  llms_full_url: docsUrl ? `${docsUrl}/llms-full.txt` : '/llms-full.txt',
};
writeFileSync(join(publicDir, 'version.json'), `${JSON.stringify(version, null, 2)}\n`);
writeFileSync(join(publicDir, 'zh-cn', 'version.json'), `${JSON.stringify(version, null, 2)}\n`);
console.log(`generated llms-full.txt from ${sources.length} canonical pages`);
console.log(`generated version.json for ${pkg.version}`);

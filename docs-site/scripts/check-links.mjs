// Source-level link/structure check for the docs site.
//
//   node scripts/check-links.mjs
//
// Validates, without a build:
//   * every internal `](/path/)` and `src="/path"` in md/mdx pages resolves to
//     a docs slug, a public asset, or an astro redirect;
//   * every `slug` item in the astro.config sidebar maps to a real page;
//   * every astro redirect target exists;
//   * local links in public/llms.txt resolve.
//
// Exit code is non-zero on any failure. Run `npm run prepare-openapi` first so
// generated public assets (openapi.json) exist.
import { readdirSync, readFileSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const siteRoot = resolve(here, '..');
const contentRoot = join(siteRoot, 'src/content/docs');
const publicRoot = join(siteRoot, 'public');

const errors = [];

// slugs for the default (root) locale, e.g. 'guides/console', 'concepts'
const slugs = new Set(['index']); // default locale
function walk(dir) {
	for (const name of readdirSync(dir, { withFileTypes: true })) {
		const full = join(dir, name.name);
		if (name.isDirectory()) {
			walk(full);
			continue;
		}
		const m = name.name.match(/^(.*)\.(md|mdx)$/);
		if (!m) continue;
		const rel = full.slice(contentRoot.length + 1).replace(/\.(md|mdx)$/, '');
		const slug = rel.replace(/\/index$/, '') || 'index';
		slugs.add(slug);
	}
}
walk(contentRoot);

// astro redirects declared in astro.config.mjs
const config = readFileSync(join(siteRoot, 'astro.config.mjs'), 'utf8');
const redirects = new Map();
const redirectsBlock = config.match(/redirects:\s*\{([^}]*)\}/s);
if (redirectsBlock) {
	for (const m of redirectsBlock[1].matchAll(/'([^']+)':\s*'([^']+)'/g)) {
		redirects.set(m[1], m[2]);
	}
}
const EXTERNAL = /^(https?:|mailto:|#|\/\/)/;

function resolves(raw, fromFile) {
	const path = raw.split('#')[0].replace(/\/+$/, '');
	if (!path) return;
	if (EXTERNAL.test(raw)) return;
	if (!path.startsWith('/')) return; // relative anchors/images handled by astro
	if (redirects.has(path)) return;
	const slug = path.slice(1);
	if (slugs.has(slug)) return;
	if (existsSync(join(publicRoot, path))) return;
	// starlight-openapi generates pages under /reference/api/
	if (/^reference\/api(\/|$)/.test(slug)) return;
	errors.push(`${fromFile}: unresolved link ${raw}`);
}

// scan content pages for internal links and local images
const LINK = /\]\(\s*(\/[^)\s]+)\s*\)|src="(\/[^"]+)"/g;
function scanContent(dir) {
	for (const name of readdirSync(dir, { withFileTypes: true })) {
		const full = join(dir, name.name);
		if (name.isDirectory()) {
			scanContent(full);
			continue;
		}
		if (!/\.(md|mdx)$/.test(name.name)) continue;
		const rel = full.slice(siteRoot.length + 1);
		const text = readFileSync(full, 'utf8');
		for (const m of text.matchAll(LINK)) {
			resolves(m[1] || m[2], rel);
		}
	}
}
scanContent(contentRoot);

// sidebar doc slugs ('a/b' string entries inside items: [...] arrays)
for (const items of config.matchAll(/items:\s*\[([^\]]*)\]/gs)) {
	for (const m of items[1].matchAll(/'([a-z0-9-]+(?:\/[a-z0-9-]+)*)'/g)) {
		const slug = m[1];
		if (!slugs.has(slug)) errors.push(`astro.config.mjs: sidebar slug '${slug}' has no page`);
	}
}

// sidebar `link:` entries must resolve
for (const m of config.matchAll(/link:\s*'(\/[^']+)'/g)) {
	resolves(m[1], 'astro.config.mjs');
}

// redirect targets must exist
for (const [from, to] of redirects) {
	const slug = to.replace(/^\//, '').replace(/\/+$/, '');
	const isGeneratedApiPage = /^reference\/api(\/|$)/.test(slug);
	if (!slugs.has(slug) && !existsSync(join(publicRoot, to)) && !isGeneratedApiPage) {
		errors.push(`astro.config.mjs: redirect ${from} -> ${to} has no target page`);
	}
}

// every llms.txt served under public/ must have resolvable local links
function* llmsFiles(dir, prefix = '') {
	for (const name of readdirSync(dir, { withFileTypes: true })) {
		if (name.isDirectory()) {
			yield* llmsFiles(join(dir, name.name), `${prefix}${name.name}/`);
		} else if (name.name === 'llms.txt') {
			yield `${prefix}${name.name}`;
		}
	}
}
for (const rel of llmsFiles(publicRoot)) {
	const file = join(publicRoot, rel);
	for (const m of readFileSync(file, 'utf8').matchAll(/\]\(([^)]+)\)/g)) {
		resolves(m[1], `public/${rel}`);
	}
}
if (!existsSync(join(publicRoot, 'llms.txt'))) errors.push('public/llms.txt is missing');

if (errors.length) {
	console.error(`check-links: ${errors.length} problem(s)`);
	for (const e of errors) console.error(`  ${e}`);
	process.exit(1);
}
console.log(`check-links: ok (${slugs.size} slugs, ${redirects.size} redirects)`);

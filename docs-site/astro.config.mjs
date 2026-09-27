// @ts-check
import { readFileSync } from 'node:fs';
import starlight from '@astrojs/starlight';
import { defineConfig } from 'astro/config';
import starlightOpenAPI, { openAPISidebarGroups } from 'starlight-openapi';

// Set DOCS_SITE_URL at build time to emit canonical URLs + sitemap.
const site = process.env.DOCS_SITE_URL || undefined;

// starlight-openapi only generates reference pages for the default locale,
// but every localized link (sidebar, language picker, /zh-cn content pages)
// prefixes the current locale — /zh-cn/reference/api/* would 404. Redirect
// each generated href back to its English page; the map is emitted by
// scripts/prepare-openapi.mjs (which always runs before build/dev).
const zhApiRedirects = (() => {
	try {
		return JSON.parse(readFileSync('.generated/api-redirects.json', 'utf8'));
	} catch {
		return {};
	}
})();

export default defineConfig({
	site,
	// Legacy slugs from the pre-foundation IA.
	redirects: {
		'/getting-started/concepts': '/concepts',
		'/guides/github': '/integrations/github',
		'/providers/overview': '/integrations/providers',
		'/providers/notes': '/integrations/provider-notes',
		'/operations/deploy': '/self-hosting/deploy',
		'/operations/configuration': '/self-hosting/configuration',
		'/operations/upgrade-and-uninstall': '/self-hosting/upgrade-and-uninstall',
		'/operations/edge': '/self-hosting/edge',
		'/operations/security': '/self-hosting/security',
		'/operations/troubleshooting': '/troubleshooting',
		...zhApiRedirects,
	},
	integrations: [
		starlight({
			title: 'sbx-browser',
			description:
				'Self-hosted orchestration for cloud coding agents: run official provider CLIs in isolated Modal Sandboxes behind one REST API.',
			logo: { src: './src/assets/logo.svg', replacesTitle: false },
			favicon: '/favicon.svg',
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/soren-labs/sbx-browser' },
			],
			editLink: {
				baseUrl: 'https://github.com/soren-labs/sbx-browser/edit/main/docs-site/',
			},
			defaultLocale: 'root',
			locales: {
				root: { label: 'English', lang: 'en' },
				'zh-cn': { label: '简体中文', lang: 'zh-CN' },
			},
			customCss: ['./src/styles/theme.css'],
			lastUpdated: true,
			plugins: [
				starlightOpenAPI([
					{
						base: 'reference/api',
						// Derived from the frozen contract by scripts/prepare-openapi.mjs.
						schema: './.generated/api-v1.yaml',
						sidebar: {
							label: 'REST API (/v1)',
							collapsed: true,
							operations: { badges: true },
						},
					},
				]),
			],
			sidebar: [
				{
					label: 'Getting started',
					translations: { 'zh-CN': '开始' },
					items: ['getting-started/introduction', 'getting-started/quick-start'],
				},
				{
					label: 'Guides',
					translations: { 'zh-CN': '使用指南' },
					items: [
						'guides/console',
						'guides/agents-and-runs',
						'guides/streaming',
						'guides/repositories',
						'guides/handoffs-and-artifacts',
						'guides/workflows',
						'guides/structured-output',
						'guides/resources-and-compute',
						'guides/accounts',
					],
				},
				{
					label: 'Concepts',
					translations: { 'zh-CN': '概念' },
					link: '/concepts/',
				},
				{
					label: 'API & SDK reference',
					translations: { 'zh-CN': 'API 与 SDK 参考' },
					items: [
						'reference/errors',
						'reference/events',
						'reference/python-client',
						'reference/cli',
						'reference/limits',
						...openAPISidebarGroups,
					],
				},
				{
					label: 'Integrations',
					translations: { 'zh-CN': '集成' },
					items: [
						'integrations/providers',
						'integrations/github',
						'integrations/provider-notes',
					],
				},
				{
					label: 'Self-hosting',
					translations: { 'zh-CN': '自托管' },
					items: [
						'self-hosting/deploy',
						'self-hosting/configuration',
						'self-hosting/upgrade-and-uninstall',
						'self-hosting/edge',
						'self-hosting/security',
					],
				},
				{
					label: 'Troubleshooting',
					translations: { 'zh-CN': '故障排查' },
					link: '/troubleshooting/',
				},
				{
					label: 'For agents',
					translations: { 'zh-CN': 'Agent 入口' },
					items: [
						'agents',
						{ label: 'llms.txt', link: '/llms.txt', attrs: { target: '_blank' } },
						{ label: 'openapi.json', link: '/openapi.json', attrs: { target: '_blank' } },
					],
				},
				{
					label: 'Project',
					translations: { 'zh-CN': '项目' },
					items: ['project/contributing', 'project/changelog'],
				},
			],
		}),
	],
});

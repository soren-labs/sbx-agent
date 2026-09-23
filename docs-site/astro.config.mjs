// @ts-check
import starlight from '@astrojs/starlight';
import { defineConfig } from 'astro/config';
import starlightOpenAPI, { openAPISidebarGroups } from 'starlight-openapi';

// Set DOCS_SITE_URL at build time to emit canonical URLs + sitemap.
const site = process.env.DOCS_SITE_URL || undefined;

export default defineConfig({
	site,
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
					label: 'Start here',
					translations: { 'zh-CN': '开始' },
					items: [
						'getting-started/introduction',
						'getting-started/quick-start',
						'getting-started/concepts',
					],
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
						'guides/github',
					],
				},
				{
					label: 'Operate',
					translations: { 'zh-CN': '部署与运维' },
					items: [
						'operations/deploy',
						'operations/configuration',
						'operations/upgrade-and-uninstall',
						'operations/edge',
						'operations/security',
						'operations/troubleshooting',
					],
				},
				{
					label: 'Providers',
					translations: { 'zh-CN': 'Provider' },
					items: ['providers/overview', 'providers/notes'],
				},
				{
					label: 'Reference',
					translations: { 'zh-CN': '参考' },
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
					label: 'Project',
					translations: { 'zh-CN': '项目' },
					items: ['project/contributing', 'project/changelog'],
				},
			],
		}),
	],
});

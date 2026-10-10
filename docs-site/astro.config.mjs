// @ts-check
import starlight from '@astrojs/starlight';
import { defineConfig } from 'astro/config';
import starlightOpenAPI, { openAPISidebarGroups } from 'starlight-openapi';

// Set DOCS_SITE_URL at build time to emit canonical URLs + sitemap.
const site = process.env.DOCS_SITE_URL || undefined;

export default defineConfig({
	site,
	redirects: {
		'/getting-started/concepts': '/concepts',
		'/operations/configuration': '/self-hosting/configuration',
		'/operations/security': '/self-hosting/security',
		'/operations/troubleshooting': '/troubleshooting',
		'/reference/errors': '/api/errors',
		'/reference/events': '/api/events',
		'/reference/python-client': '/sdk/python',
		'/sdk/python/quickstart': '/sdk/python',
		'/latest': '/',
		'/0.1': '/',
	},
	integrations: [
		starlight({
			title: 'SBX Agent',
			description:
				'Durable Sessions that run official coding-agent CLIs in isolated executors, with immutable ChangeSets and exact-subject delivery.',
			logo: { src: './src/assets/logo.svg', replacesTitle: false },
			favicon: '/favicon.svg',
			social: [
				{ icon: 'github', label: 'GitHub', href: 'https://github.com/soren-labs/sbx-agent' },
			],
			editLink: {
				baseUrl: 'https://github.com/soren-labs/sbx-agent/edit/main/docs-site/',
			},
			customCss: ['./src/styles/theme.css'],
			lastUpdated: true,
			plugins: [
				starlightOpenAPI([
					{
						base: 'reference/api',
						// Copied from docs/specs/unified/openapi.yaml by scripts/prepare-openapi.mjs.
						schema: './.generated/openapi.yaml',
						sidebar: {
							label: 'REST API (/api)',
							collapsed: true,
							operations: { badges: true },
						},
					},
				]),
			],
			sidebar: [
				{
					label: 'Getting started',
					items: ['getting-started/introduction', 'getting-started/quick-start', 'concepts'],
				},
				{
					label: 'Guides',
					items: [
						'guides/connections',
						'guides/cloud-machines',
						'guides/sessions-and-turns',
						'guides/changes-and-delivery',
						'guides/child-sessions',
						'guides/console',
					],
				},
				{
					label: 'API',
					items: ['api/overview', 'api/events', 'api/errors', ...openAPISidebarGroups],
				},
				{
					label: 'SDK and CLI',
					items: ['sdk/python', 'reference/cli', 'reference/providers'],
				},
				{
					label: 'Self-hosting',
					items: ['self-hosting/configuration', 'self-hosting/security'],
				},
				{
					label: 'Troubleshooting',
					link: '/troubleshooting/',
				},
				{
					label: 'For agents',
					items: [
						{ label: 'llms.txt', link: '/llms.txt', attrs: { target: '_blank' } },
						{ label: 'llms-full.txt', link: '/llms-full.txt', attrs: { target: '_blank' } },
						{ label: 'openapi.json', link: '/openapi.json', attrs: { target: '_blank' } },
						{ label: 'version.json', link: '/version.json', attrs: { target: '_blank' } },
					],
				},
				{
					label: 'Project',
					items: ['project/contributing', 'project/changelog'],
				},
			],
		}),
	],
});

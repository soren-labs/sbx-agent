import starlight from '@astrojs/starlight';
import {defineConfig} from 'astro/config';
import starlightOpenAPI,{openAPISidebarGroups} from 'starlight-openapi';
export default defineConfig({integrations:[starlight({title:'SBX Browser',description:'Durable Sessions, official CLI Harnesses, user-owned Executors.',plugins:[starlightOpenAPI([{base:'reference/api',schema:'../docs/specs/unified/openapi.yaml',sidebar:{label:'Unified API (/api)',collapsed:true}}])],sidebar:[{label:'Product',items:['index','setup','concepts','operations']},...openAPISidebarGroups]})]});

// Held-out query-engine oracle. Run only in the isolated verifier image.
const fs=require('node:fs'), vm=require('node:vm'), path=require('node:path'), assert=require('node:assert/strict');
const installed='/environment/repository/clients/console/node_modules/';
const ts=require('/environment/repository/tools/openapi-generator/node_modules/typescript'),{QueryClient}=require(installed+'@tanstack/react-query');
const root=process.argv[2];let client, calls=[], latest;
const error=new Error('controlled unavailable API');
const api=new Proxy({}, {get:(_,name)=>(...args)=>{calls.push({name,args});return Promise.reject(error);}});
const customRequire=(name)=>{
 if(name==='@tanstack/react-query')return {QueryClient,useQuery:options=>(latest=client.fetchQuery(options))};
 if(name==='react')return {useMemo:f=>f()};
 if(name==='./api')return api;
 if(name==='./enforcementFeed'||name==='./runsFeed')return {};
 throw Error('unexpected import '+name);
};
const source=fs.readFileSync(path.join(root,'clients/console/src/queries.ts'),'utf8');
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const subject={};vm.runInNewContext(code,{exports:subject,require:customRequire},{timeout:1000,filename:'candidate-queries.cjs'});
(async()=>{
 client=subject.createConsoleQueryClient();client.setDefaultOptions({queries:{...client.getDefaultOptions().queries,retryDelay:0}});
 let actual;try{await subject.useExecutions('selected-workflow','selected-environment');}catch(e){actual=e;}
 const results=[['bounded execution read sends one request',calls.length===1],['selected workflow/environment reach API',calls.every(c=>c.name==='fetchExecutions'&&JSON.stringify(c.args)===JSON.stringify(['selected-workflow','selected-environment']))],['unavailable error stays visible',actual===error]];
 calls=[];try{await subject.useCatalog('catalog-workflow','catalog-environment');}catch{}
 results.push(['ordinary read retains existing retry policy',calls.length===2]);client.clear();
 console.log(JSON.stringify({tests:results.map(([name,passed])=>({name,status:passed?'passed':'failed'}))}));if(results.some(x=>!x[1]))process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=2});

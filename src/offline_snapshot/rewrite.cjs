/* Build-only, syntax-aware JavaScript rewriting. No network or source execution. */
const fs = require('node:fs');
const toolRequire = process.env._OFFLINE_SNAPSHOT_NODE_ROOT
  ? require('node:module').createRequire(require('node:path').join(process.env._OFFLINE_SNAPSHOT_NODE_ROOT, 'package.json'))
  : require;
const esbuild = toolRequire('esbuild');
const {init, parse} = toolRequire('es-module-lexer/minimal');

async function main() {
  await init();
  const jobs=JSON.parse(fs.readFileSync(0,'utf8'));
  const results=[];
  for(const job of jobs) {
    try {
      if(job.checkOnly){
        await esbuild.transform(job.code,{target:'esnext',charset:'utf8',logLevel:'silent'});
        results.push({id:job.id,valid:true});continue;
      }
      const transformed=await esbuild.transform(job.code,{
        target:'esnext', charset:'utf8', legalComments:'inline', treeShaking:false,
        define:{location:'globalThis.__offlineLocation',
          window:'globalThis.__offlineWindow',
          self:'globalThis.__offlineSelf',
          'window.location':'globalThis.__offlineLocation',
          'document.location':'globalThis.__offlineLocation',
          'globalThis.location':'globalThis.__offlineLocation',
          'self.location':'globalThis.__offlineLocation',
          'document.URL':'globalThis.__offlineLocation.href',
          'document.documentURI':'globalThis.__offlineLocation.href',
          'document.baseURI':'globalThis.__offlineBase',
          'import.meta.url':JSON.stringify(job.url)}
      });
      let code=transformed.code;
      const [imports]=parse(code), edits=[];
      for(const item of imports) {
        if(item.d===-2)continue;
        if(item.d>=0) {
          // Resolve computed and literal imports alike against the source URL.
          edits.push([item.s,item.s,'globalThis.__offlineResolveModule(']);
          edits.push([item.e,item.e,','+JSON.stringify(job.base||job.url)+')']);
        } else if(item.n && /^(\.|\/|https?:)/.test(item.n)) {
          const url=new URL(item.n,job.base||job.url);url.searchParams.sort();
          edits.push([item.s,item.e,url.href]);
        }
      }
      for(const [start,end,value] of edits.sort((a,b)=>b[0]-a[0]||b[1]-a[1]))code=code.slice(0,start)+value+code.slice(end);
      results.push({id:job.id,code,warnings:transformed.warnings.map(w=>w.text)});
    } catch(error) {
      results.push({id:job.id,error:String(error.message)});
    }
  }
  process.stdout.write(JSON.stringify(results));
}
main().catch(error=>{process.stderr.write(String(error));process.exitCode=1});

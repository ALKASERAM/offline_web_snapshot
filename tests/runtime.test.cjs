const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const code=fs.readFileSync(path.join(__dirname,'../src/offline_snapshot/runtime.js'),'utf8').replace('// __OFFLINE_CSS_RUNTIME__',['css_urls.js','css_runtime.js'].map(name=>fs.readFileSync(path.join(__dirname,'../src/offline_snapshot',name),'utf8')).join('\n'));
const b64=x=>Buffer.from(x).toString('base64');
const record=(url,body,method='GET',requestBody='',mime='application/json')=>({url,body:b64(body),method,requestBody:b64(requestBody),status:200,mime,headers:{'content-type':mime}});

function runtime(entries,archiveOptions={},deferredBodies={}) {
  class Element {setAttribute(){} get innerHTML(){return this._html;} set innerHTML(v){this._html=v;}}
  const domClass=()=>class extends Element {};
  let networkCalls=0,bodyRequests=0,env;
  const listeners=new Map();
  const topBridge={postMessage(message){
    if(message?.type!=='offline-body-request')return;
    bodyRequests++;
    const encoded=deferredBodies[message.bodyRef];
    setTimeout(()=>{
      const response={type:'offline-body-response',session:message.session,requestId:message.requestId};
      if(encoded==null)response.error='Test body missing';
      else response.body=Uint8Array.from(Buffer.from(encoded,'base64')).buffer;
      for(const listener of listeners.get('message')||[])listener({source:topBridge,data:response});
    },0);
  }};
  env={console,URL,URLSearchParams,Blob,Headers,Request,Response,EventTarget,Event,TextEncoder,TextDecoder,
    Uint8Array,ArrayBuffer,atob,btoa,setTimeout,clearTimeout,Element,
    HTMLImageElement:domClass(),HTMLScriptElement:domClass(),HTMLLinkElement:domClass(),HTMLIFrameElement:domClass(),
    HTMLMediaElement:domClass(),HTMLVideoElement:domClass(),HTMLSourceElement:domClass(),
    location:new URL('file:///tmp/example.html'),history:{replaceState(){}},navigator:{},
    document:{addEventListener(){}},addEventListener(type,listener){
      if(!listeners.has(type))listeners.set(type,[]);listeners.get(type).push(listener);
    },
    fetch(){networkCalls++;throw new Error('Unexpected external fetch');},
    __OFFLINE_ARCHIVE__:{url:'https://example.org/entity/1',base:'https://example.org/',entries,routes:[],warnings:[],...archiveOptions}};
  env.window=env;env.top=Object.keys(deferredBodies).length?topBridge:env;
  vm.runInNewContext(code,env);
  return {env,networkCalls:()=>networkCalls,bodyRequests:()=>bodyRequests};
}

test('fetch matches method, canonical query and request body; misses never fall through',async()=>{
  const {env,networkCalls}=runtime([
    record('https://example.org/api?b=2&a=1','{"value":1}','POST','query=one'),
    record('https://example.org/api?a=1&b=2','{"value":2}','POST','query=two')]);
  assert.deepEqual(await (await env.fetch('//example.org/api?a=1&b=2',{method:'POST',body:'query=one'})).json(),{value:1});
  assert.deepEqual(await (await env.fetch('/api?b=2&a=1',{method:'POST',body:'query=two'})).json(),{value:2});
  await assert.rejects(env.fetch('/api?a=1&b=2'),/Not captured/);
  await assert.rejects(env.fetch('/api?a=1&b=2',{method:'POST',body:'query=absent'}),/Not captured/);
  assert.equal(networkCalls(),0);
});

test('response sequence A B A is retained then last response repeats',async()=>{
  const {env}=runtime(['A','B','A'].map(v=>record('https://example.org/api',v)));
  const results=[];for(let i=0;i<4;i++)results.push(await (await env.fetch('/api')).text());
  assert.deepEqual(results,['A','B','A','A']);
});

test('shared response body table preserves response ordering',async()=>{
  const bodies=['A','B'].map(b64);
  const entries=[0,1,0].map(bodyRef=>{const rec=record('https://example.org/api','unused');delete rec.body;rec.bodyRef=bodyRef;return rec});
  const {env}=runtime(entries,{responseBodies:bodies});
  const results=[];for(let i=0;i<4;i++)results.push(await (await env.fetch('/api')).text());
  assert.deepEqual(results,['A','B','A','A']);
});

test('deferred bodies replay through fetch and asynchronous XHR only',async()=>{
  const entries=['fetch','xhr'].map((kind,index)=>{
    const rec=record('https://example.org/'+kind,'unused');delete rec.body;
    return {...rec,bodyRef:index,resourceType:kind,xhrMode:kind==='xhr'?'async':undefined};
  });
  const options={responseBodies:[null,null],responseBodyChunks:{refs:[0,0]},bodySession:'test-session'};
  const deferred={0:b64('fetched lazily'),1:b64('xhr lazily')};
  const {env,networkCalls,bodyRequests}=runtime(entries,options,deferred);
  assert.equal(await (await env.fetch('/fetch')).text(),'fetched lazily');
  const xhr=new env.XMLHttpRequest();xhr.open('GET','/xhr');
  await new Promise((resolve,reject)=>{xhr.onload=resolve;xhr.onerror=reject;xhr.send();});
  assert.equal(xhr.responseText,'xhr lazily');
  assert.equal(bodyRequests(),2);assert.equal(networkCalls(),0);

  const guarded=runtime([entries[1]],{responseBodies:[null,null],responseBodyChunks:{refs:[null,0]},bodySession:'test-session'},deferred).env;
  const sync=new guarded.XMLHttpRequest();sync.open('GET','/xhr',false);
  assert.throws(()=>sync.send(),/Deferred response body required synchronously/);
});

test('XHR binary bodies retain bytes and emit completion events',async()=>{
  const {env,networkCalls}=runtime([record('https://example.org/image',Buffer.from([0,255,1,128]))]);
  const xhr=new env.XMLHttpRequest();xhr.open('GET','/image');xhr.responseType='arraybuffer';
  const states=[];xhr.onreadystatechange=()=>states.push(xhr.readyState);
  await new Promise((resolve,reject)=>{xhr.onload=resolve;xhr.onerror=reject;xhr.send();});
  assert.equal(xhr.status,200);assert.deepEqual(Array.from(new Uint8Array(xhr.response)),[0,255,1,128]);
  assert.deepEqual(states,[2,3,4]);assert.equal(networkCalls(),0);
});

test('XHR misses produce an error instead of silently returning an empty success',async()=>{
  const {env,networkCalls}=runtime([]);const xhr=new env.XMLHttpRequest();xhr.open('GET','/missing');
  await new Promise((resolve,reject)=>{xhr.onerror=resolve;xhr.onload=()=>reject(new Error('Unexpected success'));xhr.send();});
  assert.equal(xhr.status,0);assert.equal(xhr.readyState,4);assert.equal(networkCalls(),0);
});

test('static assets in inserted templates are rewritten before parsing',()=>{
  const {env,networkCalls}=runtime([record('https://example.org/logo.png','image')]);
  const el=new env.Element();el.innerHTML='<img src="logo.png"><img ng-src="{{logo}}"><img srcset="one.png 1x, two.png 2x">';
  assert.match(el.innerHTML,/<img src="blob:/);
  assert.match(el.innerHTML,/ng-src="\{\{logo\}\}"/);
  assert.doesNotMatch(el.innerHTML,/srcset=/);assert.equal(networkCalls(),0);
});

test('shared resource tokens are materialized in dynamic CSS without network access',async()=>{
  const css='body{background:url("offline-snapshot-resource:1")}';
  const entries=[record('https://example.org/site.css',css,'GET','','text/css'),record('https://example.org/logo.png','image','GET','','image/png')];
  const {env,networkCalls}=runtime(entries,{resourceTokens:{'offline-snapshot-resource:0':'https://example.org/site.css','offline-snapshot-resource:1':'https://example.org/logo.png'}});
  const el=new env.Element();el.innerHTML='<link rel="stylesheet" href="offline-snapshot-resource:0">';
  const href=el.innerHTML.match(/\shref="([^"]+)"/)[1];
  const compiled=await (await fetch(href)).text();
  assert.match(compiled,/url\("blob:/);
  assert.equal(networkCalls(),0);
});

test('storage changes are isolated to one replay session',()=>{
  const first=runtime([]).env;const second=runtime([]).env;
  first.localStorage.lang='en';first.localStorage.setItem('key','value');
  assert.equal(first.localStorage.getItem('lang'),'en');
  assert.equal(first.localStorage.key(1),'key');
  assert.equal(second.localStorage.getItem('key'),null);
});

test('unquoted template image attributes are embedded before the browser parses them',()=>{
  const {env}=runtime([record('https://example.org/img/logo.png','logo')]);
  const el=new env.Element();
  el.innerHTML='<img src=img/logo.png id=brand><img ng-src=img/logo_{{projectId}}.png><img srcset=remote.png>';
  assert.match(el.innerHTML,/<img src="blob:[^"]+" id=brand>/);
  assert.match(el.innerHTML,/ng-src=img\/logo_\{\{projectId\}\}\.png/);
  assert.doesNotMatch(el.innerHTML,/srcset=/);
});

test('byte ranges at the same URL are matched independently of response arrival order',async()=>{
  const last={...record('https://example.org/model',Buffer.from([2,3])),status:206,requestHeaders:{range:'bytes=2-3'},headers:{'content-range':'bytes 2-3/4'}};
  const first={...record('https://example.org/model',Buffer.from([0,1])),status:206,headers:{'content-range':'bytes 0-1/4'}};
  const {env,networkCalls}=runtime([last,first]);
  const a=await env.fetch('/model',{headers:{Range:'bytes=0-1'}});
  assert.deepEqual([...new Uint8Array(await a.arrayBuffer())],[0,1]);
  const xhr=new env.XMLHttpRequest();xhr.open('GET','/model');xhr.setRequestHeader('Range','bytes=2-3');xhr.responseType='arraybuffer';
  await new Promise((resolve,reject)=>{xhr.onload=resolve;xhr.onerror=reject;xhr.send()});
  assert.deepEqual([...new Uint8Array(xhr.response)],[2,3]);
  await assert.rejects(env.fetch('/model',{headers:{Range:'bytes=1-2'}}),/Not captured/);
  assert.equal(networkCalls(),0);
});

test('explicit resource exclusions fail fetch and XHR locally, even if a response was recorded',async()=>{
  const {env,networkCalls}=runtime([record('https://example.org/telemetry','recorded'),record('https://example.org/content','content')],{resourceExclusions:['https://example.org/telemetry*']});
  await assert.rejects(env.fetch('/telemetry?timestamp=123',{method:'POST',body:'event'}),/Explicitly excluded/);
  const xhr=new env.XMLHttpRequest();xhr.open('GET','/telemetry');
  await new Promise((resolve,reject)=>{xhr.onerror=resolve;xhr.onload=()=>reject(new Error('Excluded request succeeded'));xhr.send()});
  assert.equal(xhr.status,0);
  assert.equal(await (await env.fetch('/content')).text(),'content');
  await assert.rejects(env.fetch('/content',{method:'POST',body:'new query'}),/Not captured/);
  assert.equal(networkCalls(),0);
});

test('recorded responsive choices affect image elements only and never guess ambiguous selections',async()=>{
  const entries=[record('https://example.org/photo','large'),record('https://example.org/photo-small','small'),record('https://example.org/photo-other','other')];
  const choices=[{src:'https://example.org/photo',currentSrc:'https://example.org/photo-small'}];
  const make=items=>runtime(entries,{renderedAssets:{'https://example.org/entity/1':items}});
  const {env,networkCalls}=make(choices);
  const el=new env.Element();el.innerHTML='<img src="/photo">';
  assert.equal(await (await fetch(el.innerHTML.match(/src="([^"]+)"/)[1])).text(),'small');
  assert.equal(await (await env.fetch('/photo')).text(),'large');
  const ambiguous=make([...choices,{src:'https://example.org/photo',currentSrc:'https://example.org/photo-other'}]).env;
  const second=new ambiguous.Element();second.innerHTML='<img src="/photo">';
  assert.equal(await (await fetch(second.innerHTML.match(/src="([^"]+)"/)[1])).text(),'large');
  assert.equal(networkCalls(),0);
});

test('XHR preserves the exact bytes of sliced views and Blob bodies',async()=>{
  const body=Buffer.from([0,255,128]);
  const {env,networkCalls}=runtime([record('https://example.org/binary','matched','POST',body)]);
  for(const input of [new Uint8Array([9,0,255,128,8]).subarray(1,4),new DataView(new Uint8Array([9,0,255,128,8]).buffer,1,3),new Blob([body])]){
    const xhr=new env.XMLHttpRequest();xhr.open('POST','/binary');
    await new Promise((resolve,reject)=>{xhr.onload=resolve;xhr.onerror=()=>reject(new Error('Exact body did not match'));xhr.send(input);});
    assert.equal(xhr.status,200);assert.equal(xhr.responseText,'matched');
  }
  const changed=new env.XMLHttpRequest();changed.open('POST','/binary');
  await new Promise((resolve,reject)=>{changed.onerror=resolve;changed.onload=()=>reject(new Error('Different body matched'));changed.send(new Uint8Array([0,255,127]));});
  assert.equal(networkCalls(),0);
});

test('XHR ignores GET bodies and cancels pending Blob reads across abort and reopen',async()=>{
  const {env}=runtime([record('https://example.org/read','read'),record('https://example.org/write','written','POST','value')]);
  const xhr=new env.XMLHttpRequest();xhr.open('GET','/read');
  await new Promise((resolve,reject)=>{xhr.onload=resolve;xhr.onerror=reject;xhr.send('ignored by native XHR');});
  assert.equal(xhr.responseText,'read');
  let finish;const slow=new Blob(['value']);slow.arrayBuffer=()=>new Promise(resolve=>{finish=resolve;});
  xhr.open('POST','/write');let loads=0;xhr.onload=()=>loads++;xhr.send(slow);xhr.abort();xhr.open('GET','/read');
  await new Promise((resolve,reject)=>{xhr.onload=()=>{loads++;resolve();};xhr.onerror=reject;xhr.send();});
  finish(new TextEncoder().encode('value').buffer);await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(loads,1);assert.equal(xhr.responseText,'read');
});

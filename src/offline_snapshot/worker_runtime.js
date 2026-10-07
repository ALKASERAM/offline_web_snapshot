/* Classic-worker response replay. Original messages and transfers use a real Worker. */
(() => {
  const a=self.__OFFLINE_WORKER__,nativeFetch=self.fetch.bind(self),nativeImport=self.importScripts.bind(self);
  const bytes=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0));
  const responseBody=rec=>rec.body??a.responseBodies?.[rec.bodyRef]??'';
  const base=a.source.startsWith('blob:')?a.base:a.source;
  const url=value=>new URL(String(value),base);
  const canonical=value=>{const u=url(value);u.hash='';u.searchParams.sort();return u.href};
  let port;const pending=[];
  const report=(kind,value)=>{const item={kind,...value};if(port)port.postMessage(item);else pending.push(item)};
  function excluded(value,method){
    const target=url(value).href,pattern=(a.resourceExclusions||[]).find(p=>new RegExp('^'+p.replace(/[.+^${}()|[\]\\]/g,'\\$&').replaceAll('*','.*').replaceAll('?','.')+'$').test(target));
    if(pattern)report('excluded',{url:target,method,pattern});return Boolean(pattern);
  }
  addEventListener('message',event=>{if(event.data?.__offlineWorkerPort){port=event.ports[0];for(const item of pending)port.postMessage(item);pending.length=0;event.stopImmediatePropagation();}},true);
  addEventListener('error',event=>report('error',{message:event.message}));
  addEventListener('unhandledrejection',event=>report('error',{message:String(event.reason)}));
  self.__offlineWindow=undefined;self.__offlineSelf=self;self.__offlineLocation=new URL(base);self.__offlineBase=base;
  const key=(method,value,body='',range='')=>method.toUpperCase()+'\n'+canonical(value)+'\n'+body+'\n'+range.replace(/\s/g,'');
  const responses=new Map(),resources=new Map(),cursors=new Map(),blobs=new Map();
  for(const rec of a.entries){
    const range=rec.requestHeaders?.range||(rec.status===206?'bytes='+(/bytes\s+(\d+-\d+)\//i.exec(rec.headers['content-range']||'')?.[1]||''):'');
    const k=key(rec.method,rec.url,rec.requestBody||'',range);if(!responses.has(k))responses.set(k,[]);responses.get(k).push(rec);
    if(rec.method==='GET'&&rec.status>=200&&rec.status<300&&rec.status!==206)resources.set(canonical(rec.url),rec);
  }
  const encode=buffer=>{let s='';for(const byte of new Uint8Array(buffer))s+=String.fromCharCode(byte);return btoa(s)};
  self.fetch=async(input,init={})=>{
    const value=input instanceof Request?input.url:String(input);
    if(/^(data:|blob:)/.test(value))return nativeFetch(input,init);
    const request=new Request(input instanceof Request?input:url(value),init);
    if(excluded(value,request.method))throw new TypeError('Explicitly excluded offline worker request: '+value);
    const body=['GET','HEAD'].includes(request.method)?'':encode(await request.clone().arrayBuffer());
    const k=key(request.method,value,body,request.headers.get('range')||''), records=responses.get(k);
    if(!records){report('miss',{method:request.method,url:url(value).href});throw new TypeError('Worker request not captured: '+value)}
    const at=cursors.get(k)||0;cursors.set(k,at+1);const rec=records[Math.min(at,records.length-1)];report('hit',{});
    const headers=new Headers(rec.headers);headers.delete('content-length');headers.delete('content-encoding');
    const response=new Response([204,205,304].includes(rec.status)?null:bytes(responseBody(rec)),{status:rec.status,headers});
    Object.defineProperty(response,'url',{value:rec.url});return response;
  };
  function script(value){
    if(/^(blob:|data:)/.test(String(value)))return value;
    if(excluded(value,'SCRIPT'))return URL.createObjectURL(new Blob([''],{type:'application/javascript'}));
    const name=canonical(value);if(blobs.has(name))return blobs.get(name);
    const rec=resources.get(name);if(!rec){report('miss',{method:'RESOURCE',url:name});throw new TypeError('Worker script not captured: '+name)}
    const result=URL.createObjectURL(new Blob([bytes(responseBody(rec))],{type:'application/javascript'}));blobs.set(name,result);return result;
  }
  self.importScripts=(...urls)=>nativeImport(...urls.map(script));
  for(const kind of ['XMLHttpRequest','WebSocket','EventSource','Worker','SharedWorker'])self[kind]=function(){report('unsupported',{api:kind});throw new Error('Nested worker API not supported: '+kind)};
  self.importScripts(a.source);
})();

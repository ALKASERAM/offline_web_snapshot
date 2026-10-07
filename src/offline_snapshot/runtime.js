/* Offline Snapshot 0.1. Original site scripts run against recorded resources. */
(() => {
  'use strict';
  const archive = window.__OFFLINE_ARCHIVE__;
  const native = {setAttribute: Element.prototype.setAttribute, removeAttribute:Element.prototype.removeAttribute, fetch: window.fetch, Worker:window.Worker, XMLHttpRequest:window.XMLHttpRequest};
  const nativeGetAttribute=Element.prototype.getAttribute,scriptSources=new WeakMap(),linkSources=new WeakMap();
  const source = new URL(archive.url);
  let virtualURL = new URL(archive.url), virtualState=archive.historyState??null;
  const stats = {hits: 0, misses: [], errors: [], blocked: [], violations: []};
  // A local file must not inherit another snapshot's browser storage. This is
  // session-local state; authentication and IndexedDB are outside this release.
  function memoryStorage(seed={}) {
    const data=new Map(Object.entries(seed).map(([k,v])=>[k,String(v)]));
    const api={get length(){return data.size;},key:i=>Array.from(data.keys())[i]??null,
      getItem:k=>data.get(String(k))??null,setItem:(k,v)=>data.set(String(k),String(v)),
      removeItem:k=>data.delete(String(k)),clear:()=>data.clear()};
    return new Proxy(api,{get:(t,k)=>k in t?t[k]:data.get(String(k)),set:(t,k,v)=>{data.set(String(k),String(v));return true;},deleteProperty:(t,k)=>data.delete(String(k))});
  }
  for(const kind of ['localStorage','sessionStorage']) {
    try{Object.defineProperty(window,kind,{configurable:true,value:memoryStorage(archive.storage?.[kind]||{})});}
    catch{stats.errors.push('Could not isolate '+kind);}
  }
  const resources = new Map(), responses = new Map(), cursors = new Map(), blobs = new Map();
  const bytes = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  const storedBody=rec=>rec.body??archive.responseBodies?.[rec.bodyRef];
  const deferredBody=rec=>archive.responseBodyChunks?.refs?.[rec.bodyRef]!=null;
  const responseBody = rec => {
    const value=storedBody(rec);
    if(value==null&&deferredBody(rec))throw new Error('Deferred response body required synchronously: '+rec.url);
    return value??'';
  };
  const bodyRequests=new Map();let bodyRequestSequence=0;
  addEventListener('message',event=>{
    const message=event.data;
    if(event.source!==window.top||message?.type!=='offline-body-response'||message.session!==archive.bodySession)return;
    const pending=bodyRequests.get(message.requestId);if(!pending)return;
    bodyRequests.delete(message.requestId);clearTimeout(pending.timer);
    if(message.error)pending.reject(new Error(message.error));
    else pending.resolve(new Uint8Array(message.body));
  });
  const responseBytesAsync=rec=>{
    const value=storedBody(rec);
    if(value!=null)return Promise.resolve(bytes(value));
    if(!deferredBody(rec))return Promise.resolve(new Uint8Array());
    if(!archive.bodySession||window.top===window)return Promise.reject(new Error('Deferred response body store is unavailable'));
    const requestId=String(++bodyRequestSequence);
    return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{bodyRequests.delete(requestId);reject(new Error('Deferred response body timed out: '+rec.url));},30000);
      bodyRequests.set(requestId,{resolve,reject,timer});
      window.top.postMessage({type:'offline-body-request',session:archive.bodySession,requestId,bodyRef:rec.bodyRef},'*');
    });
  };
  const b64 = b => {let s=''; for(const v of b) s+=String.fromCharCode(v); return btoa(s);};
  const textBytes = s => new TextEncoder().encode(s);
  function absolute(value) {
    let s=String(value);
    // URLs resolved by the browser against the local file need their virtual origin.
    if (s.startsWith('file:')) {
      const u=new URL(s), root=new URL('.', location.href).pathname;
      s=u.pathname.startsWith(root) ? u.pathname.slice(root.length)+u.search : u.pathname+u.search;
    }
    return new URL(s, archive.scriptTransforms ? (archive.documentBase || virtualURL.href) : (archive.base || archive.url));
  }
  function canonical(value) {
    const u=absolute(value); u.hash=''; u.searchParams.sort(); return u.href;
  }
  const rangeOf=rec=>rec.requestHeaders?.range||(rec.status===206?('bytes='+(/bytes\s+(\d+-\d+)\//i.exec(rec.headers['content-range']||'')?.[1]||'')):'');
  const key=(method,url,body='',range='')=>method.toUpperCase()+'\n'+canonical(url)+'\n'+body+'\n'+range.replace(/\s/g,'');
  for (const rec of archive.entries) {
    const k=key(rec.method,rec.url,rec.requestBody||'',rangeOf(rec));
    if(!responses.has(k)) responses.set(k,[]);
    responses.get(k).push(rec);
    if(rec.method==='GET' && rec.status>=200 && rec.status<300&&rec.status!==206) resources.set(canonical(rec.url),rec);
  }
  if(archive.scriptTransforms) {
    // A source URL is a JavaScript-facing value; the real browser stays on the
    // local file / its embedded documents. Parser transforms opt source code in.
    Object.defineProperty(window,'__offlineBase',{get:()=>archive.documentBase||virtualURL.href});
    const virtualLocation=new Proxy({}, {
      get(_,name){
        if(name==='toString'||name==='valueOf'||name===Symbol.toPrimitive)return ()=>virtualURL.href;
        if(name==='assign')return value=>navigate(value);
        if(name==='replace')return value=>navigate(value,true);
        if(name==='reload')return ()=>parent.postMessage({type:'offline-reload'},'*');
        return virtualURL[name];
      },
      set(_,name,value){
        const next=new URL(virtualURL);next[name]=value;
        if(name==='hash'){
          const old=virtualURL.href;changeHistory('pushState',virtualState,next.href);
          dispatchEvent(new HashChangeEvent('hashchange',{oldURL:old,newURL:virtualURL.href}));
        } else navigate(next.href);
        return true;
      }
    });
    Object.defineProperty(window,'__offlineLocation',{get:()=>virtualLocation,set:value=>navigate(value),configurable:true});
    const boundMethods=new Map();
    const virtualWindow=new Proxy(window,{
      get(target,key){
        if(key==='window'||key==='self')return virtualWindow;
        if(key==='location')return virtualLocation;
        if(archive.embeddedNavigation&&(key==='parent'||key==='top'))return virtualWindow;
        const value=Reflect.get(target,key,target);
        if(typeof value==='function'&&!value.prototype){
          if(!boundMethods.has(value))boundMethods.set(value,value.bind(target));
          return boundMethods.get(value);
        }
        return value;
      },
      set(target,key,value){if(key==='location')return !!navigate(value);return Reflect.set(target,key,value,target);}
    });
    window.__offlineWindow=virtualWindow;
    window.__offlineSelf=virtualWindow;
    // Libraries also invoke prototype methods with window as their receiver.
    // A Proxy has no native EventTarget brand; unwrap only our virtual window.
    for(const name of ['addEventListener','removeEventListener','dispatchEvent']){
      const method=EventTarget.prototype[name];
      EventTarget.prototype[name]=function(...args){return Reflect.apply(method,this===virtualWindow?window:this,args);};
    }
    // URL parsers in older frameworks use an <a> element. A blob document has
    // no hierarchical base, so expose the recorded base through these getters.
    for(const prop of ['href','protocol','host','hostname','port','pathname','search','hash','origin','username','password']){
      const descriptor=Object.getOwnPropertyDescriptor(HTMLAnchorElement.prototype,prop);
      if(descriptor?.get)Object.defineProperty(HTMLAnchorElement.prototype,prop,{...descriptor,get(){
        const value=nativeGetAttribute.call(this,'href');
        if(value===null||/^(data:|blob:|javascript:|mailto:|tel:)/i.test(value))return descriptor.get.call(this);
        try{return absolute(value)[prop];}catch{return descriptor.get.call(this);}
      }});
    }
    function changeHistory(method,state,value) {
      const next=value==null?new URL(virtualURL):new URL(String(value),virtualURL);
      if(next.origin!==source.origin)throw new DOMException('Source origin must be preserved','SecurityError');
      const cloned=structuredClone(state);
      if(archive.embeddedNavigation){
        parent.postMessage({type:'offline-history',method,url:next.href,state:cloned},'*');
      } else if(!archive.subframe){
        nativeHistory[method](cloned,'','#offline-url='+encodeURIComponent(next.href));
      }
      virtualURL=next;virtualState=cloned;
    }
    const nativeHistory={pushState:history.pushState.bind(history),replaceState:history.replaceState.bind(history)};
    history.pushState=(state,unused,value)=>changeHistory('pushState',state,value);
    history.replaceState=(state,unused,value)=>changeHistory('replaceState',state,value);
    Object.defineProperty(history,'state',{configurable:true,get:()=>virtualState});
    if(archive.embeddedNavigation){
      history.back=()=>parent.postMessage({type:'offline-history-go',delta:-1},'*');
      history.forward=()=>parent.postMessage({type:'offline-history-go',delta:1},'*');
      history.go=delta=>parent.postMessage({type:'offline-history-go',delta:Number(delta)||0},'*');
      addEventListener('message',event=>{
        if(event.source!==parent||event.data?.type!=='offline-popstate')return;
        const old=virtualURL.href;virtualURL=new URL(event.data.url);virtualState=event.data.state;
        dispatchEvent(new PopStateEvent('popstate',{state:virtualState}));
        if(new URL(old).hash!==virtualURL.hash)dispatchEvent(new HashChangeEvent('hashchange',{oldURL:old,newURL:virtualURL.href}));
      });
    }
    const moduleImports={}, moduleScopes={}, capturedMap=archive.importMap||{};
    for(const [url,rec] of resources) {
      // API payloads can advertise JavaScript or JSON without being modules.
      // Browser module dependencies are recorded as script resources; fetch/XHR
      // responses must remain on their asynchronous replay path.
      if(rec.resourceType==='fetch'||rec.resourceType==='xhr')continue;
      if(!/javascript|ecmascript|json/.test(rec.mime))continue;
      const blob=URL.createObjectURL(new Blob([bytes(responseBody(rec))],{type:rec.mime}));
      blobs.set(url,blob);moduleImports[url]=blob;
    }
    const resolveTarget=value=>value==null?null:(moduleImports[canonical(new URL(value,archive.base||archive.url).href)]||new URL(value,archive.base||archive.url).href);
    const mapped=table=>{
      const result={};
      for(const [k,v] of Object.entries(table||{})){
        const name=/^(\.|\/|https?:)/.test(k)?new URL(k,archive.base||archive.url).href:k;
        result[name]=resolveTarget(v);
        if(k.endsWith('/')&&typeof v==='string'&&v.endsWith('/')){
          const prefix=new URL(v,archive.base||archive.url).href;
          for(const [url,blob] of blobs)if(url.startsWith(prefix))result[name+url.slice(prefix.length)]=blob;
        }
      }
      return result;
    };
    Object.assign(moduleImports,mapped(capturedMap.imports));
    const scopes=Object.entries(capturedMap.scopes||{}).map(([scope,values])=>[new URL(scope,archive.base||archive.url).href,values]).sort((a,b)=>a[0].length-b[0].length);
    for(const [url,blob] of blobs) {
      for(const [prefix,values] of scopes)if(url.startsWith(prefix))moduleScopes[blob]={...moduleScopes[blob],...mapped(values)};
    }
    window.__offlineResolveModule=(specifier,importer)=>{
      const spec=String(specifier);
      if(/^(\.|\/|https?:)/.test(spec))return canonical(new URL(spec,importer).href);
      let mapping={...capturedMap.imports};
      for(const [prefix,values] of scopes)if(importer.startsWith(prefix))Object.assign(mapping,values);
      const key=Object.keys(mapping).filter(k=>k===spec||(k.endsWith('/')&&spec.startsWith(k))).sort((a,b)=>b.length-a.length)[0];
      if(!key||mapping[key]==null)throw new TypeError('Unresolved recorded module: '+spec);
      return canonical(new URL(mapping[key]+spec.slice(key.length),archive.base||archive.url).href);
    };
    // Native import maps preserve cycles, shared module identity and async import.
    const map=document.createElement('script');map.type='importmap';map.textContent=JSON.stringify({imports:moduleImports,scopes:moduleScopes});document.head.appendChild(map);
  }
  function lookup(method,url,body='',headers={}) {
    const range=new Headers(headers).get('range')||'';
    const k=key(method,url,body,range), candidates=responses.get(k);
    if(!candidates) {stats.misses.push({method,url:absolute(url).href}); update(); return null;}
    const i=cursors.get(k)||0; cursors.set(k,i+1); stats.hits++; update();
    // Replay a recorded sequence, then retain its last known response.
    return candidates[Math.min(i,candidates.length-1)];
  }
  const resourcePattern=url=>(archive.resourceExclusions||[]).find(pattern=>new RegExp('^'+pattern.replace(/[.+^${}()|[\]\\]/g,'\\$&').replaceAll('*','.*').replaceAll('?','.')+'$').test(absolute(url).href));
  const imageSelections=new Map();
  for(const item of archive.renderedAssets?.[archive.url]||archive.renderedAssets?.[canonical(archive.url)]||[]){
    if(!item.src||!item.currentSrc||/^(blob:|data:)/i.test(item.currentSrc))continue;
    const src=canonical(item.src),chosen=canonical(item.currentSrc);
    if(!imageSelections.has(src))imageSelections.set(src,chosen);
    else if(imageSelections.get(src)!==chosen)imageSelections.set(src,null);
  }
  function excludedRequest(url,method){
    const pattern=resourcePattern(url);if(!pattern)return false;
    stats.blocked.push({kind:'explicit resource exclusion',method,url:absolute(url).href,pattern});update();return true;
  }
  function materializeResourceTokens(value) {
    return String(value).replace(/offline-snapshot-resource:\d+/g,token=>{
      const source=archive.resourceTokens?.[token];
      return source?asset(source):'data:,';
    });
  }
  function asset(value,kind='asset') {
    let s=String(value);
    if(archive.resourceTokens?.[s])s=archive.resourceTokens[s];
    if(!s || /^(data:|blob:|#)/i.test(s) || (kind==='iframe'&&s==='about:blank')) return s;
    const excluded=resourcePattern(s);
    if(excluded){stats.blocked.push({kind:'explicit resource exclusion',url:s,pattern:excluded});update();return kind==='script'?'data:application/javascript,':'data:,';}
    if(kind==='iframe') {stats.blocked.push({kind,url:s});update();return 'about:blank';}
    let k;try{k=canonical(s);}catch{return 'data:,';}
    // Hydration and later DOM insertion must use the same observed responsive
    // image as initial markup. Only an unambiguous, recorded image selection
    // qualifies; fetch/XHR keys and other resource types remain exact.
    if(kind==='img'&&imageSelections.get(k)&&resources.has(imageSelections.get(k)))k=imageSelections.get(k);
    if(blobs.has(k)) return blobs.get(k);
    const rec=resources.get(k);
    if(!rec){stats.misses.push({method:'RESOURCE',url:k,kind});update();return kind==='img'?'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg"/>':'data:,';}
    let body=bytes(responseBody(rec));
    if(/text\/css/i.test(rec.mime))body=textBytes(materializeResourceTokens(new TextDecoder().decode(body)));
    const u=URL.createObjectURL(new Blob([body],{type:rec.mime}));blobs.set(k,u);return u;
  }
  // __OFFLINE_CSS_RUNTIME__
  function shouldRewrite(el,name) {
    return (name==='src' && /^(IMG|SCRIPT|IFRAME|SOURCE|VIDEO|AUDIO|INPUT)$/.test(el.tagName)) ||
      (name==='href' && el.tagName==='LINK') || name==='poster';
  }
  function linkKind(rel){
    const tokens=String(rel||'').toLowerCase().split(/\s+/);
    if(tokens.some(t=>['stylesheet','icon','apple-touch-icon','apple-touch-icon-precomposed','mask-icon'].includes(t)))return 'asset';
    if(tokens.some(t=>['preconnect','dns-prefetch','prefetch','preload','modulepreload','manifest'].includes(t)))return 'hint';
    return 'metadata';
  }
  function linkSource(el){return linkSources.get(el)??nativeGetAttribute.call(el,'data-offline-link-href');}
  function updateLink(el){
    const value=linkSource(el);if(value==null)return;
    const kind=linkKind(nativeGetAttribute.call(el,'rel'));
    if(!linkSources.has(el)&&kind===nativeGetAttribute.call(el,'data-offline-link-kind'))return;
    // Once a compiled link changes kind, future transitions must remap it,
    // including a transition back to its original stylesheet relation.
    linkSources.set(el,value);
    const target=kind==='asset'?asset(value,'link'):kind==='metadata'?'data:text/css,':null;
    if(target===null){if(nativeGetAttribute.call(el,'href')!==null)native.removeAttribute.call(el,'href');}
    else if(nativeGetAttribute.call(el,'href')!==target)native.setAttribute.call(el,'href',target);
  }
  function frameDocument(value){try{return archive.frameDocuments?.[canonical(value)];}catch{return null;}}
  const frameWritten=new WeakMap();
  function initializeFrame(element){
    const key=nativeGetAttribute.call(element,'data-offline-frame-url');
    if(!key||!element.isConnected)return;
    const html=frameDocument(key),doc=element.contentDocument;
    if(!html||!doc)return;
    const previous=frameWritten.get(element);
    if(previous?.key===key&&previous.document===doc)return;
    frameWritten.set(element,{key,document:doc});
    // about:blank inherits the parent origin and honours the source doctype.
    // srcdoc always uses standards mode, breaking legacy frame canvas sizing.
    doc.open();doc.write(materializeResourceTokens(html));doc.close();
  }
  function setFrame(element,value){
    if(!frameDocument(value))return false;
    native.setAttribute.call(element,'data-offline-frame-url',canonical(value));
    if(!nativeGetAttribute.call(element,'src'))native.setAttribute.call(element,'src','about:blank');
    initializeFrame(element);return true;
  }
  if(archive.scriptTransforms){
    new MutationObserver(()=>document.querySelectorAll('iframe[data-offline-frame-url]').forEach(initializeFrame)).observe(document,{childList:true,subtree:true,attributes:true,attributeFilter:['data-offline-frame-url']});
    document.addEventListener('load',event=>{if(event.target.tagName==='IFRAME')initializeFrame(event.target);},true);
  }
  Element.prototype.setAttribute=function(name,value){
    if(name.toLowerCase()==='style')value=cssValue(value);
    if(this.tagName==='LINK'&&name.toLowerCase()==='imagesrcset')value='';
    if(this.tagName==='LINK'&&name.toLowerCase()==='href'){linkSources.set(this,String(value));updateLink(this);return;}
    if(this.tagName==='LINK'&&name.toLowerCase()==='rel'){native.setAttribute.call(this,name,value);updateLink(this);return;}
    if(archive.scriptTransforms&&name.toLowerCase()==='autofocus')name='data-offline-autofocus';
    if(this.tagName==='IFRAME'&&name.toLowerCase()==='src'&&setFrame(this,value))return;
    if(archive.scriptTransforms&&this.tagName==='SCRIPT'&&name.toLowerCase()==='src'&&!/^(data:|blob:)/.test(String(value)))scriptSources.set(this,absolute(value).href);
    if(shouldRewrite(this,name.toLowerCase())) value=asset(value,this.tagName.toLowerCase());
    if(name.toLowerCase()==='srcset') value='';
    return native.setAttribute.call(this,name,value);
  };
  Element.prototype.removeAttribute=function(name){
    if(this.tagName==='LINK'&&String(name).toLowerCase()==='href'){
      linkSources.delete(this);native.removeAttribute.call(this,'data-offline-link-href');
    }
    native.removeAttribute.call(this,name);
    if(this.tagName==='LINK'&&String(name).toLowerCase()==='rel')updateLink(this);
  };
  if(archive.scriptTransforms){
    Element.prototype.getAttribute=function(name){
      if(this.tagName==='LINK'&&String(name).toLowerCase()==='href')return linkSource(this)??nativeGetAttribute.call(this,name);
      if(this.tagName==='SCRIPT'&&String(name).toLowerCase()==='src')return scriptSources.get(this)||nativeGetAttribute.call(this,'data-offline-source')||nativeGetAttribute.call(this,name);
      if(this.tagName==='BASE'&&String(name).toLowerCase()==='href'&&archive.baseAttribute!=null)return archive.baseAttribute;
      return nativeGetAttribute.call(this,name);
    };
    const attrValue=Object.getOwnPropertyDescriptor(Attr.prototype,'value');
    if(attrValue?.get)Object.defineProperty(Attr.prototype,'value',{...attrValue,get(){
      const element=this.ownerElement;
      if(this.name==='href'&&element?.tagName==='LINK')return linkSource(element)??attrValue.get.call(this);
      if(this.name==='src'&&element?.tagName==='SCRIPT')return scriptSources.get(element)||nativeGetAttribute.call(element,'data-offline-source')||attrValue.get.call(this);
      return attrValue.get.call(this);
    },set(value){
      const element=this.ownerElement;
      if(this.name==='href'&&element?.tagName==='LINK'){linkSources.set(element,String(value));updateLink(element);return;}
      if(this.name==='src'&&element?.tagName==='SCRIPT'){
        scriptSources.set(element,absolute(value).href);value=asset(value,'script');
      }
      attrValue.set.call(this,value);
    }});
  }
  function markup(value) {
    return cssMarkup(value).replace(/<iframe\b[^>]*>/gi,tag=>{
      const match=tag.match(/\s+src\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/i);
      const url=match&&(match[1]??match[2]??match[3]).replace(/&amp;/gi,'&');
      const frame=url&&frameDocument(url);
      // A source framework may compile every attribute. Keep child JavaScript
      // out of those attributes (e.g. its {{ strings are not parent templates).
      return frame?tag.replace(match[0],()=>' src="about:blank" data-offline-frame-url="'+canonical(url).replaceAll('&','&amp;').replaceAll('"','&quot;')+'"'):tag;
    }).replace(/<(img|script|link|iframe|source|video|audio|input)\b[^>]*>/gi,(tag,kind)=>{
      if(kind.toLowerCase()==='iframe'&&/\s(?:srcdoc|data-offline-frame-url)\s*=/.test(tag))return tag;
      if(archive.scriptTransforms)tag=tag.replace(/\s+autofocus(?=\s|=|>)(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?/gi,' data-offline-autofocus');
      return tag.replace(/\s+(?:srcset|imagesrcset)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi,'').replace(/(\s)(src|poster|href)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi,(match,space,attr,double,single,unquoted)=>{
        const url=double??single??unquoted, quote='"';
        if((attr.toLowerCase()==='href'&&kind.toLowerCase()!=='link')||url.includes('{{'))return match;
        if(kind.toLowerCase()==='link'&&attr.toLowerCase()==='href'){
          const rel=tag.match(/\srel\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/i),type=linkKind(rel&&(rel[1]??rel[2]??rel[3]));
          const source=url.replace(/&amp;/gi,'&'),escape=s=>String(s).replaceAll('&','&amp;').replaceAll('"','&quot;');
          return ' data-offline-link-href="'+escape(absolute(source).href)+'" data-offline-link-kind="'+type+'"'+(type==='hint'?'':' href="'+escape(type==='metadata'?'data:text/css,':asset(source,'link'))+'"');
        }
        const rewritten=asset(url.replace(/&amp;/gi,'&'),kind.toLowerCase());
        return space+attr+'='+quote+rewritten.replaceAll('&','&amp;').replaceAll(quote,quote==='"'?'&quot;':'&#39;')+quote;
      });});
  }
  // Angular/JQuery insert static template assets through HTML strings, bypassing
  // src setters. Rewrite those before the browser's parser sees the markup.
  for(const prop of ['innerHTML','outerHTML']) {
    const d=Object.getOwnPropertyDescriptor(Element.prototype,prop);
    if(d?.set)Object.defineProperty(Element.prototype,prop,{...d,set(value){d.set.call(this,prop==='innerHTML'&&this.tagName==='STYLE'?cssValue(value):markup(value));}});
  }
  const adjacent=Element.prototype.insertAdjacentHTML;
  if(adjacent)Element.prototype.insertAdjacentHTML=function(position,value){return adjacent.call(this,position,markup(value));};
  if(archive.scriptTransforms){
    const rel=Object.getOwnPropertyDescriptor(HTMLLinkElement.prototype,'rel');
    if(rel?.set)Object.defineProperty(HTMLLinkElement.prototype,'rel',{...rel,set(value){rel.set.call(this,value);updateLink(this);}});
    new MutationObserver(records=>{
      for(const record of records){
        if(record.type==='attributes')updateLink(record.target);
        else for(const node of record.addedNodes){
          if(node.nodeType!==1)continue;
          if(node.tagName==='LINK')updateLink(node);
          node.querySelectorAll('link').forEach(updateLink);
        }
      }
    }).observe(document,{subtree:true,childList:true,attributes:true,attributeFilter:['rel']});
  }
  for(const klass of [HTMLImageElement,HTMLSourceElement]){
    const descriptor=Object.getOwnPropertyDescriptor(klass.prototype,'srcset');
    if(descriptor?.set)Object.defineProperty(klass.prototype,'srcset',{...descriptor,set(){descriptor.set.call(this,'');}});
  }
  const imagePreload=Object.getOwnPropertyDescriptor(HTMLLinkElement.prototype,'imageSrcset');
  if(imagePreload?.set)Object.defineProperty(HTMLLinkElement.prototype,'imageSrcset',{...imagePreload,set(){imagePreload.set.call(this,'');}});
  if(archive.scriptTransforms){
    // Chromium refuses native autofocus in the file's cross-origin container.
    // Explicit DOM focus preserves the page's initial input without changing
    // browser security settings or stealing focus after user interaction.
    let focused=false,queued=false;
    const observer=new MutationObserver(()=>{
      if(focused||queued)return;queued=true;
      requestAnimationFrame(()=>{
        queued=false;
        const input=[...document.querySelectorAll('[data-offline-autofocus]')].find(el=>!el.disabled&&el.getClientRects().length);
        if(!input)return;
        focused=true;observer.disconnect();
        if(!document.activeElement||document.activeElement===document.body)input.focus();
      });
    });
    observer.observe(document,{childList:true,subtree:true,attributes:true,attributeFilter:['data-offline-autofocus']});
    document.addEventListener('pointerdown',()=>{focused=true;observer.disconnect();},{once:true});
    document.addEventListener('keydown',()=>{focused=true;observer.disconnect();},{once:true});
  }
  for(const [klass,prop] of [[HTMLImageElement,'src'],[HTMLScriptElement,'src'],[HTMLLinkElement,'href'],[HTMLIFrameElement,'src'],[HTMLMediaElement,'src'],[HTMLVideoElement,'poster'],[HTMLSourceElement,'src']]) {
    const d=Object.getOwnPropertyDescriptor(klass.prototype,prop);
    if(d?.set) Object.defineProperty(klass.prototype,prop,{...d,
      get(){if(archive.scriptTransforms&&this.tagName==='SCRIPT')return scriptSources.get(this)||nativeGetAttribute.call(this,'data-offline-source')||d.get.call(this);if(this.tagName==='LINK'&&linkSource(this)!=null)return absolute(linkSource(this)).href;return d.get.call(this);},
      set(v){if(this.tagName==='LINK'){linkSources.set(this,String(v));updateLink(this);return;}if(this.tagName==='IFRAME'&&setFrame(this,v))return;if(archive.scriptTransforms&&this.tagName==='SCRIPT'&&!/^(data:|blob:)/.test(String(v)))scriptSources.set(this,absolute(v).href);d.set.call(this,asset(v,this.tagName.toLowerCase()));}});
  }
  async function requestBody(input,init) {
    if(init?.body!=null) {
      if(typeof init.body==='string') return b64(textBytes(init.body));
      if(init.body instanceof URLSearchParams) return b64(textBytes(init.body.toString()));
      if(init.body instanceof ArrayBuffer) return b64(new Uint8Array(init.body));
      if(ArrayBuffer.isView(init.body)) return b64(new Uint8Array(init.body.buffer,init.body.byteOffset,init.body.byteLength));
      if(init.body instanceof Blob) return b64(new Uint8Array(await init.body.arrayBuffer()));
      throw new TypeError('Offline replay does not support this request body type');
    }
    return input instanceof Request && !['GET','HEAD'].includes(input.method) ? b64(new Uint8Array(await input.clone().arrayBuffer())) : '';
  }
  window.fetch=async function(input,init={}) {
    const u=input instanceof Request?input.url:String(input), method=init.method||(input instanceof Request?input.method:'GET');
    if(excludedRequest(u,method))throw new TypeError('Explicitly excluded offline request: '+u);
    if(/^(blob:|data:)/i.test(u)) return native.fetch.call(window,input,init);
    const rec=lookup(method,u,await requestBody(input,init),init.headers||(input instanceof Request?input.headers:{}));
    if(!rec) throw new TypeError('Not captured: '+u);
    const h=new Headers(rec.headers);h.delete('content-encoding');h.delete('content-length');
    const r=new Response([101,204,205,304].includes(rec.status)?null:await responseBytesAsync(rec),{status:rec.status,headers:h});
    Object.defineProperty(r,'url',{value:rec.url}); return r;
  };
  class ReplayXHR extends EventTarget {
    constructor(){super();this.readyState=0;this.status=0;this.statusText='';this.response=null;this.responseText='';this.responseType='';this.responseURL='';this.timeout=0;this.withCredentials=false;this.upload=new EventTarget();this._headers={};this._aborted=false;this._generation=0;}
    _event(name,original){const e=original&&typeof ProgressEvent!=='undefined'&&'loaded' in original?new ProgressEvent(name,{lengthComputable:original.lengthComputable,loaded:original.loaded,total:original.total}):new Event(name);this.dispatchEvent(e);if(typeof this['on'+name]==='function')this['on'+name](e);}
    open(method,url,async=true,user,password){
      this._generation++;if(this._local)this._local.abort();this._local=null;
      this._method=String(method).toUpperCase();this._url=url;this._async=async;this._headers={};this._rec=null;this.status=0;this.response=null;this.responseText='';this._aborted=false;this.readyState=1;
      // Runtime-created object URLs are browser-local values, not recorded HTTP
      // requests. Native XHR preserves binary/range/revocation/event semantics.
      // Only these non-network schemes may reach it; HTTP stays exact replay.
      if(native.XMLHttpRequest&&['blob:','data:'].includes(absolute(url).protocol)){
        const local=this._local=new native.XMLHttpRequest(),generation=this._generation;
        for(const name of ['readystatechange','loadstart','progress','load','error','abort','timeout','loadend'])local.addEventListener(name,event=>{if(generation===this._generation)this._event(name,event);});
        for(const name of ['loadstart','progress','load','error','abort','timeout','loadend'])local.upload.addEventListener(name,event=>{
          if(generation!==this._generation)return;const copy=new ProgressEvent(name,{lengthComputable:event.lengthComputable,loaded:event.loaded,total:event.total});this.upload.dispatchEvent(copy);if(typeof this.upload['on'+name]==='function')this.upload['on'+name](copy);
        });
        local.open(method,absolute(url).href,async,user,password);
        for(const name of ['responseType','timeout','withCredentials'])if(this['_'+name])local[name]=this['_'+name];
        return;
      }
      this._event('readystatechange');
    }
    setRequestHeader(k,v){this._headers[k.toLowerCase()]=v;if(this._local)this._local.setRequestHeader(k,v);}
    overrideMimeType(v){this._mime=v;if(this._local)this._local.overrideMimeType(v);}
    getAllResponseHeaders(){if(this._local)return this._local.getAllResponseHeaders();return this._rec?Object.entries(this._rec.headers).filter(([k])=>!['set-cookie','content-encoding','content-length'].includes(k.toLowerCase())).map(([k,v])=>k+': '+v).join('\r\n'):'';}
    getResponseHeader(k){if(this._local)return this._local.getResponseHeader(k);return this._rec?Object.entries(this._rec.headers).find(([n])=>n.toLowerCase()===k.toLowerCase())?.[1]??null:null;}
    abort(){if(this._local){this._local.abort();this._generation++;return;}this._generation++;this._aborted=true;this.status=0;this.readyState=0;this._event('abort');this._event('loadend');}
    send(body=null){
      const generation=this._generation,omitted=excludedRequest(this._url,this._method);
      if(this._local&&!omitted){this._local.send(body);return;}
      if(this._local){this._local.abort();this._local=null;}
      if(['GET','HEAD'].includes(this._method)||omitted)body=null;
      const encode=value=>{
        if(value==null)return '';
        if(typeof value==='string'||value instanceof URLSearchParams)return b64(textBytes(String(value)));
        if(value instanceof ArrayBuffer)return b64(new Uint8Array(value));
        if(ArrayBuffer.isView(value))return b64(new Uint8Array(value.buffer,value.byteOffset,value.byteLength));
        throw new TypeError('Unsupported XHR body');
      };
      const fail=()=>{if(this._aborted||generation!==this._generation)return;this.status=0;this.readyState=4;this._event('readystatechange');this._event('error');this._event('loadend');};
      const select=b=>{if(this._aborted||generation!==this._generation)return null;this._event('loadstart');const rec=this._rec=omitted?null:lookup(this._method,this._url,b,this._headers);if(!rec)fail();return rec;};
      const finish=(rec,data)=>{if(this._aborted||generation!==this._generation)return;this.status=rec.status;this.statusText=rec.status===200?'OK':'';this.responseURL=rec.url;
        this.readyState=2;this._event('readystatechange');this.readyState=3;this._event('readystatechange');
        this.responseText=new TextDecoder().decode(data);
        if(this.responseType==='arraybuffer')this.response=data.buffer;
        else if(this.responseType==='blob')this.response=new Blob([data],{type:rec.mime});
        else if(this.responseType==='json'){try{this.response=JSON.parse(this.responseText);}catch{this.response=null;}}
        else if(this.responseType==='document'){this.response=new DOMParser().parseFromString(this.responseText,/xml/.test(rec.mime)?'application/xml':'text/html');this.responseXML=this.response;}
        else this.response=this.responseText;
        this.readyState=4;this._event('readystatechange');this._event('load');this._event('loadend');};
      const run=async b=>{const rec=select(b);if(!rec)return;try{finish(rec,await responseBytesAsync(rec));}catch(error){stats.errors.push('Deferred XHR body: '+error.message);update();fail();}};
      if(body instanceof Blob){
        if(!this._async)throw new TypeError('Synchronous Blob XHR bodies are not supported');
        body.arrayBuffer().then(value=>run(encode(value)),()=>{
          if(this._aborted||generation!==this._generation)return;
          this.status=0;this.readyState=4;this._event('readystatechange');this._event('error');this._event('loadend');
        });
      } else {const encoded=encode(body);if(this._async)setTimeout(()=>run(encoded),0);else {const rec=select(encoded);if(rec)finish(rec,bytes(responseBody(rec)));}}
    }
  }
  for(const name of ['readyState','status','statusText','response','responseText','responseXML','responseURL','responseType','timeout','withCredentials'])Object.defineProperty(ReplayXHR.prototype,name,{
    configurable:true,get(){return this._local?this._local[name]:this['_'+name];},
    set(value){this['_'+name]=value;if(this._local&&['responseType','timeout','withCredentials'].includes(name))this._local[name]=value;}
  });
  for(const [k,v] of Object.entries({UNSENT:0,OPENED:1,HEADERS_RECEIVED:2,LOADING:3,DONE:4})) {ReplayXHR[k]=v;ReplayXHR.prototype[k]=v;}
  window.XMLHttpRequest=ReplayXHR;
  const unavailable=kind=>function(url){stats.blocked.push({kind,url:String(url)});update();throw new Error(kind+' is not supported in this capture');};
  window.WebSocket=unavailable('WebSocket');window.EventSource=unavailable('EventSource');window.Worker=unavailable('Worker');window.SharedWorker=unavailable('SharedWorker');
  if(archive.workerRuntime&&native.Worker){
    window.Worker=function(value,options={}){
      if(options.type==='module'){stats.blocked.push({kind:'Worker',url:String(value),reason:'Module worker graph not supported'});update();throw new Error('Module worker graph not captured');}
      const source=/^(blob:|data:)/.test(String(value))?String(value):absolute(value).href;
      const payload={source,base:archive.documentBase||virtualURL.href,entries:archive.entries,responseBodies:archive.responseBodies,resourceExclusions:archive.resourceExclusions||[]};
      const code='self.__OFFLINE_WORKER__='+JSON.stringify(payload)+';\n'+archive.workerRuntime;
      const bootstrap=URL.createObjectURL(new Blob([code],{type:'application/javascript'}));
      const worker=new native.Worker(bootstrap,options),channel=new MessageChannel();
      channel.port1.onmessage=event=>{
        const item=event.data;
        if(item.kind==='hit')stats.hits++;
        else if(item.kind==='miss')stats.misses.push({...item,worker:source});
        else if(item.kind==='excluded')stats.blocked.push({...item,kind:'explicit resource exclusion',worker:source});
        else if(item.kind==='error')stats.errors.push('Worker: '+item.message);
        else stats.blocked.push({kind:'Worker',url:source,reason:item.api+' in worker is unsupported'});
        update();
      };
      worker.addEventListener('error',event=>{stats.errors.push('Worker: '+event.message);update();});
      worker.postMessage({__offlineWorkerPort:true},[channel.port2]);
      const terminate=worker.terminate.bind(worker);worker.terminate=()=>{channel.port1.close();URL.revokeObjectURL(bootstrap);terminate();};
      return worker;
    };
    window.Worker.prototype=native.Worker.prototype;
  }
  if(navigator.sendBeacon)navigator.sendBeacon=function(url){if(!excludedRequest(url,'BEACON'))stats.blocked.push({kind:'beacon',url:String(url)});update();return false;};
  if(navigator.serviceWorker)navigator.serviceWorker.register=async()=>{throw new Error('Service workers unavailable in standalone replay');};
  let panel=null, message='';
  function update(){if(panel){const omitted=stats.blocked.filter(x=>x.kind==='explicit resource exclusion').length;panel.querySelector('[data-summary]').textContent=stats.hits+' responses replayed · '+stats.misses.length+' missing'+(omitted?' · '+omitted+' explicitly omitted':'');panel.querySelector('pre').textContent=JSON.stringify({collectionMethod:archive.collectionMethod,...stats,warnings:archive.warnings,scriptTransforms:archive.scriptTransforms,resourceExclusions:archive.resourceExclusions,resourceOmissions:archive.resourceOmissions,assetCollection:archive.assetCollection,privacyAudit:archive.privacyAudit,scrollActions:archive.scrollActions,crawl:archive.crawl},null,2);panel.querySelector('[data-message]').textContent=message;}}
  function blockedNavigation(url){message='This destination was not captured: '+url;stats.blocked.push({kind:'navigation',url});if(panel)panel.open=true;update();}
  const routes=new Set(archive.routes||[]);
  function navigate(value,replace=false){
    const u=absolute(value);
    if(archive.embeddedNavigation){
      const anchor=u.hash;
      if(!/^#!?\//.test(u.hash))u.hash='';u.searchParams.sort();
      const target=archive.pageAliases?.[u.href]||u.href;
      if(archive.pageURLs?.includes(target)){
        parent.postMessage({type:'offline-navigate',url:target+(u.hash?'':anchor),replace},'*');return true;
      }
      blockedNavigation(u.href);return false;
    }
    if(u.origin!==source.origin||!routes.has(u.pathname+u.search)){blockedNavigation(u.href);return false;}
    blockedNavigation(u.href);return false;
  }
  function linkClick(e){const a=e.target.closest?.('a[href]');if(!a||a.hasAttribute('data-offline-static')||e.defaultPrevented)return;const href=a.getAttribute('href');if(href==='#'){e.preventDefault();return;}if(!href||href.startsWith('javascript:'))return;if(href.startsWith('#')){if(archive.scriptTransforms){e.preventDefault();window.__offlineLocation.hash=href;document.getElementById(href.slice(1))?.scrollIntoView();}return;}e.preventDefault();if(!archive.scriptTransforms)e.stopImmediatePropagation();navigate(href);}
  // Let the original app's router handle clicks first. Plain document links
  // reach the window after application listeners and use embedded navigation.
  (archive.scriptTransforms?window:document).addEventListener('click',linkClick,!archive.scriptTransforms);
  document.addEventListener('submit',e=>{e.preventDefault();message='Form submission is not enabled for this capture.';update();},true);
  window.open=url=>{navigate(url);return null;};
  window.addEventListener('error',e=>{stats.errors.push(e.message||'Resource error');update();});
  window.addEventListener('unhandledrejection',e=>{stats.errors.push(String(e.reason));update();});
  document.addEventListener('securitypolicyviolation',e=>{stats.violations.push({directive:e.violatedDirective,uri:e.blockedURI});update();});
  if(archive.initialHash&&!location.hash.startsWith('#!'))history.replaceState(null,'','#'+archive.initialHash);
  document.addEventListener('DOMContentLoaded',()=>{
    panel=document.createElement('details');panel.id='offline-snapshot-status';
    panel.style.cssText='position:fixed;bottom:8px;left:8px;z-index:2147483647;background:#fff;border:1px solid #789;padding:6px 10px;max-width:450px;max-height:45vh;overflow:auto;font:12px system-ui;color:#123;box-shadow:0 2px 12px #0002';
    if(archive.subframe)panel.style.display='none';
    panel.innerHTML='<summary>Snapshot report</summary><strong>Offline snapshot replay</strong><div data-summary></div><div data-message role="status"></div><details><summary>Capture report</summary><pre style="white-space:pre-wrap"></pre></details>';
    if(archive.crawl){
      const scope=document.createElement('div');
      const recorded=archive.crawl.pages.filter(p=>p.status==='recorded').length;
      scope.textContent='Link depth '+archive.crawl.depth+' · '+recorded+' URL targets recorded'+(archive.crawl.limitReached?' · page limit reached':'');
      if(archive.crawl.includePaths?.length)scope.textContent+=' · paths: '+archive.crawl.includePaths.join(', ');
      panel.appendChild(scope);
    }
    if(archive.staticViews?.length){
      const views=document.createElement('details'),summary=document.createElement('summary');
      summary.textContent='Saved layouts (static fallback)';views.appendChild(summary);
      for(const view of archive.staticViews){const link=document.createElement('a');link.textContent=view.name;link.href=URL.createObjectURL(new Blob([materializeResourceTokens(view.html)],{type:'text/html'}));link.target='_blank';link.rel='noopener';link.setAttribute('data-offline-static','');link.style.display='block';views.appendChild(link);}
      panel.appendChild(views);
    }
    document.body.appendChild(panel);update();
    if(archive.embeddedNavigation)parent.postMessage({type:'offline-title',title:document.title},'*');
  });
})();

/* Per-file, in-memory browser state. No personal browser storage is accessed. */
(() => {
  const archive=window.__OFFLINE_ARCHIVE__;
  if(!archive.scriptTransforms)return;
  // Blob frames loaded by a file can have opaque origins. Do not read the
  // parent Window; cross-document cookie state travels through postMessage.
  const sessions=window.__offlineSessions||(window.__offlineSessions={});
  window.__offlineSessions=sessions;
  const origin=new URL(archive.url).origin;
  const session=sessions[origin]||(sessions[origin]={cookies:archive.cookieSeed||[],caches:new Map()});
  const source=()=>new URL(window.__offlineLocation?.href||archive.url);
  Object.defineProperty(document,'cookie',{configurable:true,
    get(){const u=source();return session.cookies.filter(c=>c.expires>Date.now()&&(u.hostname===c.domain||(!c.hostOnly&&u.hostname.endsWith('.'+c.domain)))&&(u.pathname===c.path||u.pathname.startsWith(c.path.endsWith('/')?c.path:c.path+'/'))&&(!c.secure||u.protocol==='https:')).sort((a,b)=>b.path.length-a.path.length).map(c=>c.name+'='+c.value).join('; ');},
    set(value){
      const [pair,...parts]=String(value).split(';'), at=pair.indexOf('=');if(at<1)return;
      const u=source(), attrs=Object.fromEntries(parts.map(p=>{const i=p.indexOf('=');return i<0?[p.trim().toLowerCase(),true]:[p.slice(0,i).trim().toLowerCase(),p.slice(i+1).trim()]}));
      if(attrs.httponly)return;
      const domain=typeof attrs.domain==='string'?attrs.domain.replace(/^\./,'').toLowerCase():u.hostname;
      if(u.hostname!==domain&&!u.hostname.endsWith('.'+domain))return;
      const path=typeof attrs.path==='string'&&attrs.path.startsWith('/')?attrs.path:u.pathname.slice(0,u.pathname.lastIndexOf('/')+1)||'/';
      const item={name:pair.slice(0,at).trim(),value:pair.slice(at+1).trim(),domain,path,hostOnly:!attrs.domain,secure:!!attrs.secure,expires:Number.MAX_SAFE_INTEGER};
      if(item.secure&&u.protocol!=='https:')return;
      if(typeof attrs.expires==='string'&&!Number.isNaN(Date.parse(attrs.expires)))item.expires=Date.parse(attrs.expires);
      if(typeof attrs['max-age']==='string'&&/^-?\d+$/.test(attrs['max-age']))item.expires=Date.now()+Number(attrs['max-age'])*1000;
      session.cookies=session.cookies.filter(c=>!(c.name===item.name&&c.domain===domain&&c.path===path));
      if(item.expires>Date.now())session.cookies.push(item);
      if(archive.embeddedNavigation)parent.postMessage({type:'offline-cookie-state',cookies:session.cookies},'*');
    }
  });
  const request=input=>input instanceof Request?input:new Request(new URL(String(input),window.__offlineBase||source()).href);
  function matches(record,input,options={}) {
    const req=request(input), a=new URL(record.request.url),b=new URL(req.url);
    if(!options.ignoreMethod&&req.method!=='GET')return false;
    if(options.ignoreSearch){a.search='';b.search='';}a.hash='';b.hash='';
    if(a.href!==b.href)return false;
    if(!options.ignoreVary){for(const header of (record.response.headers.get('vary')||'').split(',').map(s=>s.trim()).filter(Boolean)){if(header==='*'||record.request.headers.get(header)!==req.headers.get(header))return false;}}
    return true;
  }
  function cache(records) {return {
    async match(input,options){return records.find(r=>matches(r,input,options))?.response.clone();},
    async matchAll(input,options){return records.filter(r=>input===undefined||matches(r,input,options)).map(r=>r.response.clone());},
    async put(input,response){const req=request(input);if(req.method!=='GET'||response.status===206||(response.headers.get('vary')||'').split(',').some(h=>h.trim()==='*'))throw new TypeError('Uncacheable request or response');const stored=response.clone();const i=records.findIndex(r=>matches(r,req,{ignoreVary:true}));if(i>=0)records.splice(i,1);records.push({request:req.clone(),response:stored});},
    async delete(input,options){let removed=false;for(let i=records.length-1;i>=0;i--)if(matches(records[i],input,options)){records.splice(i,1);removed=true;}return removed;},
    async keys(input,options){return records.filter(r=>input===undefined||matches(r,input,options)).map(r=>r.request.clone());},
    async add(input){const req=request(input),response=await fetch(req);if(!response.ok)throw new TypeError('Cache fetch failed');await this.put(req,response);},
    async addAll(inputs){for(const input of inputs)await this.add(input);}
  };}
  Object.defineProperty(window,'caches',{configurable:true,value:{
    async open(name){name=String(name);if(!session.caches.has(name))session.caches.set(name,[]);return cache(session.caches.get(name));},
    async has(name){return session.caches.has(String(name));},
    async delete(name){return session.caches.delete(String(name));},
    async keys(){return [...session.caches.keys()];},
    async match(input,options={}){for(const [name,records] of session.caches){if(options.cacheName&&options.cacheName!==name)continue;const value=await cache(records).match(input,options);if(value)return value;}}
  }});
})();

/* Decode the standalone bundle outside the JavaScript parser. */
(() => {
  'use strict';
  const payload=document.getElementById('offline-snapshot-bundle');
  function decodeBase64(value){
    const padding=value.endsWith('==')?2:value.endsWith('=')?1:0;
    const result=new Uint8Array(value.length/4*3-padding),chunk=4*16384;
    let written=0;
    for(let offset=0;offset<value.length;offset+=chunk){
      const decoded=atob(value.slice(offset,offset+chunk));
      for(let index=0;index<decoded.length;index++)result[written++]=decoded.charCodeAt(index);
    }
    return result;
  }
  async function decodePayload(encoded){
    const compressed=decodeBase64(encoded);
    const stream=new Blob([compressed]).stream().pipeThrough(new DecompressionStream('gzip'));
    return JSON.parse(await new Response(stream).text());
  }
  function createBodyStore(config){
    const references=config?.refs||[],descriptors=config?.chunks||[];
    const cache=new Map(),limit=config?.cacheLimitBytes||0;
    let cachedBytes=0,loads=0,hits=0,evictions=0;
    function trim(current){
      while(cache.size>1&&cachedBytes>limit){
        const oldest=cache.keys().next().value;
        if(oldest===current){
          const value=cache.get(oldest);cache.delete(oldest);cache.set(oldest,value);
          continue;
        }
        const removed=cache.get(oldest);cache.delete(oldest);
        cachedBytes-=removed.bytes;evictions++;
      }
    }
    async function chunk(index){
      if(cache.has(index)){
        const existing=cache.get(index);cache.delete(index);cache.set(index,existing);
        hits++;return existing.promise;
      }
      const descriptor=descriptors[index];
      const node=document.getElementById('offline-snapshot-body-'+index);
      if(!descriptor||!node)throw new Error('Deferred response chunk is missing: '+index);
      const entry={bytes:descriptor.encodedBodyBytes||0};
      entry.promise=decodePayload(node.textContent.trim()).then(values=>{
        loads++;cachedBytes+=entry.bytes;trim(index);return new Map(values);
      },error=>{cache.delete(index);throw error;});
      cache.set(index,entry);return entry.promise;
    }
    return {
      async get(reference){
        const index=references[reference];
        if(index==null)throw new Error('Deferred response body is not indexed: '+reference);
        const values=await chunk(index);
        if(!values.has(reference))throw new Error('Deferred response body is missing: '+reference);
        return values.get(reference);
      },
      stats(){return {loads,hits,evictions,cachedChunks:cache.size,cachedBytes,limit};},
    };
  }
  window.__OFFLINE_BUNDLE_READY__=(async()=>{
    if(typeof DecompressionStream!=='function')throw new Error('This browser does not support gzip decompression required by this offline snapshot.');
    const encoded=payload.textContent.trim();
    payload.textContent='';payload.remove();
    const bundle=await decodePayload(encoded);
    window.__OFFLINE_BODY_STORE__=createBodyStore(bundle.archive.responseBodyChunks);
    window.__OFFLINE_BUNDLE__=bundle;
    return bundle;
  })();
})();

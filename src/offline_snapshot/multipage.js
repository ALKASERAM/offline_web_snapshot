/* Single-file container for captured full-document navigation. */
(async() => {
  'use strict';
  const bundle=await window.__OFFLINE_BUNDLE_READY__,documents=bundle.documents;
  const bodyStore=window.__OFFLINE_BODY_STORE__;
  let frame,activeDocument=null,pendingLoad=null,activeLoadId=null,loadSequence=0;
  const materializedResources=new Map(),resourceTokens=bundle.archive.resourceTokens||{};
  const bytes=value=>Uint8Array.from(atob(value),c=>c.charCodeAt(0));
  const responseBody=record=>{
    const value=record.body??bundle.archive.responseBodies?.[record.bodyRef];
    if(value==null&&bundle.archive.responseBodyChunks?.refs?.[record.bodyRef]!=null)
      throw new Error('Deferred response body required synchronously: '+record.url);
    return value??'';
  };
  const base64=value=>{let result='';for(const byte of value)result+=String.fromCharCode(byte);return btoa(result)};
  const canonical=value=>{const u=new URL(value);u.hash='';u.searchParams.sort();return u.href};
  const resources=new Map(bundle.archive.entries.filter(item=>item.method==='GET'&&item.status>=200&&item.status<300&&item.status!==206).map(item=>[canonical(item.url),item]));
  const tokenPattern=/offline-snapshot-resource:\d+/g;
  function resource(token){
    if(materializedResources.has(token))return materializedResources.get(token);
    const url=resourceTokens[token],record=url&&resources.get(canonical(url));
    if(!record)return 'data:,';
    let body=responseBody(record);
    if(/text\/css/i.test(record.mime))body=base64(new TextEncoder().encode(materialize(new TextDecoder().decode(bytes(body)))));
    const result='data:'+record.mime.split(';')[0]+';base64,'+body;
    materializedResources.set(token,result);return result;
  }
  const materialize=value=>String(value).replace(tokenPattern,token=>resource(token));
  const cookieState={};
  const key=value=>{const u=new URL(value);if(!/^#!?\//.test(u.hash))u.hash='';u.searchParams.sort();return u.href;};
  const aliases=bundle.archive.pageAliases||{};
  const resolve=value=>{const k=key(value);return documents[k]?k:aliases[k];};
  function loader(id){
    const source=`<!doctype html><meta charset="utf-8"><script>
addEventListener('message',function receive(event){
  const value=event.data;
  if(event.source!==parent||value?.type!=='offline-document'||value.id!==${JSON.stringify(id)})return;
  removeEventListener('message',receive);
  try{
    window.__OFFLINE_ARCHIVE__=value.archive;
    document.open();document.write(value.html);document.close();
  }catch(error){
    parent.postMessage({type:'offline-loader-error',id:value.id,message:String(error?.message||error)},'*');
  }
});
function ready(){parent.postMessage({type:'offline-loader-ready',id:${JSON.stringify(id)}},'*');}
if(document.readyState==='loading')addEventListener('DOMContentLoaded',ready,{once:true});else ready();
</scr${''}ipt>`;
    return URL.createObjectURL(new Blob([source],{type:'text/html'}));
  }
  function show(fromHistory=false) {
    let requested;
    try{requested=decodeURIComponent(location.hash.slice(1))||bundle.archive.url;}catch{requested=bundle.archive.url;}
    const url=history.state?.offlineDocument||resolve(requested);
    if(!url)return;
    if(fromHistory&&activeDocument===url){
      frame.contentWindow.postMessage({type:'offline-popstate',url:requested,state:history.state?.appState??null},'*');return;
    }
    const cookieSeed=cookieState[new URL(requested).origin]||documents[url].settings?.cookieSeed||[];
    const page=documents[url],id=String(++loadSequence);
    const archive={...bundle.archive,...page.settings,url:requested,base:page.base,initialHash:null,embeddedNavigation:true,cookieSeed,historyState:history.state?.appState??null,bodySession:id};
    const documentHTML=materialize(page.html).replace(bundle.marker,()=>bundle.runtime);
    const loaderURL=loader(id);
    if(pendingLoad)URL.revokeObjectURL(pendingLoad.loaderURL);
    pendingLoad={id,archive,html:documentHTML,loaderURL};
    // Keep history in the file's hash, without extra iframe history entries.
    activeDocument=url;
    frame.contentWindow.location.replace(loaderURL+new URL(requested).hash);
  }
  addEventListener('message',event=>{
    const message=event.data;
    if(message?.type==='offline-body-request'){
      if(!bodyStore||message.session!==activeLoadId)return;
      const target=event.source,requestId=message.requestId;
      bodyStore.get(message.bodyRef).then(encoded=>{
        const value=bytes(encoded);
        target?.postMessage({type:'offline-body-response',session:message.session,requestId,body:value.buffer},'*',[value.buffer]);
      },error=>target?.postMessage({type:'offline-body-response',session:message.session,requestId,error:String(error?.message||error)},'*'));
      return;
    }
    if(!frame||event.source!==frame.contentWindow)return;
    if(message?.type==='offline-loader-ready'&&pendingLoad?.id===message.id){
      const value=pendingLoad;pendingLoad=null;
      activeLoadId=value.id;
      try{frame.contentWindow.postMessage({type:'offline-document',id:value.id,archive:value.archive,html:value.html},'*');}
      catch(error){showError(error.message);}
      setTimeout(()=>URL.revokeObjectURL(value.loaderURL),0);
      return;
    }
    if(message?.type==='offline-loader-error'&&activeLoadId===message.id){showError(message.message);return;}
    if(message?.type==='offline-navigate'&&resolve(message.url)){
      const doc=resolve(message.url);
      history[message.replace?'replaceState':'pushState']({offlineDocument:doc},'','#'+encodeURIComponent(message.url));show();
    }
    if(message?.type==='offline-history'&&new URL(message.url).origin===new URL(bundle.archive.url).origin){
      if(!['pushState','replaceState'].includes(message.method))return;
      history[message.method]({offlineDocument:activeDocument,appState:message.state},'','#'+encodeURIComponent(message.url));
    }
    if(message?.type==='offline-history-go')history.go(message.delta);
    if(message?.type==='offline-reload')show();
    if(message?.type==='offline-cookie-state'&&activeDocument)cookieState[new URL(activeDocument).origin]=message.cookies;
    if(message?.type==='offline-title'){
      document.title=message.title;
      document.getElementById('offline-snapshot-loading')?.remove();
    }
  });
  addEventListener('popstate',()=>show(true));
  function showError(message){
    console.error('Offline snapshot could not open its captured document:',message);
    const status=document.getElementById('offline-snapshot-loading');
    if(status)status.textContent='Offline snapshot could not open: '+message;
  }
  function start(){
    frame=document.getElementById('offline-page');
    if(!location.hash)history.replaceState({offlineDocument:resolve(bundle.archive.url)},'','#'+encodeURIComponent(bundle.archive.url));
    show();
  }
  if(document.readyState==='loading')addEventListener('DOMContentLoaded',start,{once:true});else start();
})().catch(error=>{
  console.error('Offline snapshot could not start',error);
  const status=document.getElementById('offline-snapshot-loading');
  if(status)status.textContent='Offline snapshot could not start: '+error.message;
});

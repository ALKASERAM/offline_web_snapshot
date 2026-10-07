// Browser hooks. The packager embeds this file inside runtime.js's closure.
function cssSource(sheet){
  if(!sheet)return undefined;
  const node=sheet.ownerNode;
  if(node?.tagName==='LINK')return linkSource(node)||undefined;
  return undefined;
}
function cssValue(value,base){
  return rewriteCSS(value,url=>{
    if(!url||/^(?:data:|blob:|#)/i.test(url))return url;
    let resolved;try{resolved=base?new URL(url,base):absolute(url);}catch{return url;}
    const fragment=resolved.hash;resolved.hash='';return asset(resolved.href,'css')+fragment;
  });
}
function cssMarkup(value){
  const s=String(value);if(!/style[\s=>]/i.test(s))return s;
  const decode=s=>s.replace(/&(?:amp|quot|apos|lt|gt|#x[\da-f]+|#\d+);/gi,entity=>{
    const key=entity.slice(1,-1).toLowerCase();
    if(key[0]==='#'){const cp=parseInt(key.slice(key[1]==='x'?2:1),key[1]==='x'?16:10);return cp>0&&cp<=0x10ffff?String.fromCodePoint(cp):'\ufffd';}
    return {amp:'&',quot:'"',apos:"'",lt:'<',gt:'>'}[key];
  });
  return s.replace(/<style\b[^>]*>[\s\S]*?<\/style\s*>|<[a-z][^>]*>/gi,tag=>{
    if(/^<style\b/i.test(tag))return tag.replace(/(>)([\s\S]*)(<\/style\s*>)/i,(_,open,text,close)=>open+cssValue(text)+close);
    return tag.replace(/(\s)style\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi,(original,space,double,single,unquoted)=>{
      const before=decode(double??single??unquoted),after=cssValue(before);
      return before===after?original:space+'style="'+after.replaceAll('&','&amp;').replaceAll('"','&quot;')+'"';
    });
  });
}
if(typeof CSSStyleDeclaration!=='undefined'){
  const declarations=new WeakMap(),targets=new WeakMap(),unwrap=value=>targets.get(value)||value;
  function declarationProxy(style){
    if(!style)return style;
    if(!declarations.has(style)){
      const methods=new Map();
      const proxy=new Proxy(style,{
        get(target,key){const value=Reflect.get(target,key,target);if(typeof value!=='function'||key==='constructor')return value;if(!methods.has(value))methods.set(value,value.bind(target));return methods.get(value);},
        set(target,key,value){return Reflect.set(target,key,cssValue(value,cssSource(target.parentRule?.parentStyleSheet)),target);}
      });
      declarations.set(style,proxy);targets.set(proxy,style);
    }
    return declarations.get(style);
  }
  const declaration=CSSStyleDeclaration.prototype,property=declaration.setProperty;
  declaration.setProperty=function(name,value,priority){const target=unwrap(this);return property.call(target,name,cssValue(value,cssSource(target.parentRule?.parentStyleSheet)),priority);};
  // Engines put named CSS properties on different prototype levels.
  for(let proto=Object.getPrototypeOf(document.documentElement.style);proto&&proto!==Object.prototype;proto=Object.getPrototypeOf(proto)){
    for(const key of Object.getOwnPropertyNames(proto)){
      const d=Object.getOwnPropertyDescriptor(proto,key);
      if(!d?.configurable)continue;
      if(d.set||d.get)Object.defineProperty(proto,key,{...d,...(d.get?{get(){return d.get.call(unwrap(this));}}:{}),...(d.set?{set(value){const target=unwrap(this);return d.set.call(target,cssValue(value,cssSource(target.parentRule?.parentStyleSheet)));}}:{})});
      else if(typeof d.value==='function'&&key!=='constructor'){const method=d.value;Object.defineProperty(proto,key,{...d,value:function(...args){return method.apply(unwrap(this),args);}});}
    }
  }
  // Chromium exposes CSS names as native own properties, bypassing prototype
  // setters. Proxy only declaration objects obtained through a style getter.
  for(const klass of [window.HTMLElement,window.SVGElement,window.CSSStyleRule,window.CSSFontFaceRule,window.CSSKeyframeRule,window.CSSPageRule]){
    const d=klass&&Object.getOwnPropertyDescriptor(klass.prototype,'style');
    if(d?.get&&d.configurable)Object.defineProperty(klass.prototype,'style',{...d,get(){return declarationProxy(d.get.call(this));},...(d.set?{set(value){return d.set.call(this,cssValue(value,cssSource(this.parentStyleSheet)));}}:{})});
  }
  for(const [klass,names] of [[window.CSSStyleSheet,['insertRule','replace','replaceSync']],[window.CSSGroupingRule,['insertRule']],[window.CSSKeyframesRule,['appendRule']]]){
    if(!klass)continue;
    for(const name of names){const method=klass.prototype[name];if(method)klass.prototype[name]=function(text,...args){return method.call(this,cssValue(text,cssSource(this.parentStyleSheet||this)),...args);};}
  }
  const text=Object.getOwnPropertyDescriptor(Node.prototype,'textContent');
  const isStyle=node=>node?.nodeType===1&&node.localName==='style';
  Object.defineProperty(Node.prototype,'textContent',{...text,set(value){return text.set.call(this,cssValueIfStyle(this,value));}});
  function cssValueIfStyle(node,value){return isStyle(node)||isStyle(node.parentNode)?cssValue(value):value;}
  for(const [proto,name] of [[Node.prototype,'nodeValue'],[CharacterData.prototype,'data'],[HTMLElement.prototype,'innerText']]){
    const d=Object.getOwnPropertyDescriptor(proto,name);
    if(d?.set)Object.defineProperty(proto,name,{...d,set(value){return d.set.call(this,cssValueIfStyle(this,value));}});
  }
  function prepare(node,parent){
    if(typeof node==='string')return isStyle(parent)?cssValue(node):node;
    if(!node)return node;
    if(isStyle(parent)&&node.nodeType===3){const before=text.get.call(node),after=cssValue(before);if(after!==before)text.set.call(node,after);}
    if(node.nodeType===1||node.nodeType===11){
      const styles=[...(isStyle(node)?[node]:[]),...node.querySelectorAll('style')];
      for(const el of styles){const before=text.get.call(el),after=cssValue(before);if(after!==before)text.set.call(el,after);}
      for(const el of [...(node.nodeType===1&&node.hasAttribute('style')?[node]:[]),...node.querySelectorAll('[style]')]){
        const before=nativeGetAttribute.call(el,'style'),after=cssValue(before);if(after!==before)native.setAttribute.call(el,'style',after);
      }
    }
    return node;
  }
  for(const name of ['appendChild','insertBefore','replaceChild']){
    const method=Node.prototype[name];Node.prototype[name]=function(node,...args){return method.call(this,prepare(node,this),...args);};
  }
  for(const klass of [Element,DocumentFragment,Document])for(const name of ['append','prepend','replaceChildren']){
    const method=klass.prototype[name];if(method)klass.prototype[name]=function(...nodes){return method.apply(this,nodes.map(n=>prepare(n,this)));};
  }
}

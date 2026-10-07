/* CSS URL tokens are rewritten before native setters can start a request.
 * CSS Syntax 3: comments/strings are not URL tokens; escapes belong to URLs.
 * This is a resource-token scanner, not a CSS validator or evaluator. */
function rewriteCSS(input, resolveURL) {
  if(typeof input!=='string'||!/(?:url|image-set|@import|\\)/i.test(input))return input;
  const s=input,n=s.length,frames=[];let i=0,out='',importURL=false;
  const space=c=>c!==undefined&&/[\t\n\f\r ]/.test(c);
  const name=c=>c!==undefined&&/[\w\-\u0080-\uffff]/.test(c);
  function escape(at){
    let end=at+1;
    if(/[0-9a-f]/i.test(s[end]||'!')){
      let hex='';while(hex.length<6&&/[0-9a-f]/i.test(s[end]||'!'))hex+=s[end++];
      if(space(s[end])){if(s[end]==='\r'&&s[end+1]==='\n')end++;end++;}
      const cp=parseInt(hex,16);return {end,value:!cp||cp>0x10ffff||(cp>=0xd800&&cp<=0xdfff)?'\ufffd':String.fromCodePoint(cp)};
    }
    if(s[end]==='\r'&&s[end+1]==='\n')return {end:end+2,value:''};
    if(/[\r\n\f]/.test(s[end]||'!'))return {end:end+1,value:''};
    return {end:Math.min(end+1,n),value:s[end]||''};
  }
  function ident(at){let value='',end=at;while(end<n){if(s[end]==='\\'){const e=escape(end);value+=e.value;end=e.end;}else if(name(s[end]))value+=s[end++];else break;}return {end,value};}
  function string(at){
    const quote=s[at];let value='',end=at+1;
    while(end<n){if(s[end]===quote)return {end:end+1,value,valid:true};if(s[end]==='\\'){const e=escape(end);value+=e.value;end=e.end;}else if(/[\r\n\f]/.test(s[end]))break;else value+=s[end++];}
    return {end,value,valid:false};
  }
  const quoted=value=>'"'+String(value).replace(/\\/g,'\\\\').replace(/"/g,'\\"').replace(/[\n\r\f]/g,c=>'\\'+c.charCodeAt(0).toString(16)+' ')+'"';
  const mapped=(value,original)=>{const next=resolveURL(value);return next===value?original:quoted(next);};
  while(i<n){
    const start=i,ch=s[i],frame=frames.at(-1);
    if(s.startsWith('/*',i)){const end=s.indexOf('*/',i+2);i=end<0?n:end+2;out+=s.slice(start,i);continue;}
    if(space(ch)){out+=ch;i++;continue;}
    if(ch==='"'||ch==="'"){
      const token=string(i);i=token.end;
      out+=token.valid&&(importURL||(frame?.image&&frame.first))?mapped(token.value,s.slice(start,i)):s.slice(start,i);
      if(frame)frame.first=false;importURL=false;continue;
    }
    if(ch==='@'||ch==='#'){
      const token=ident(i+1);i=token.end;out+=s.slice(start,i);importURL=ch==='@'&&token.value.toLowerCase()==='import';continue;
    }
    if(name(ch)||ch==='\\'){
      const token=ident(i);i=token.end;const lower=token.value.toLowerCase();
      if(s[i]==='('&&lower==='url'){
        let end=i+1,value='',valid=true;while(space(s[end]))end++;
        if(s[end]==='"'||s[end]==="'"){const t=string(end);value=t.value;end=t.end;valid=t.valid;}
        else{
          while(end<n&&s[end]!==')'&&!space(s[end])){
            if(s[end]==='\\'){if(/[\n\r\f]/.test(s[end+1]||'!')){valid=false;break;}const e=escape(end);value+=e.value;end=e.end;}
            else if(/["'(\x00-\x08\x0b\x0e-\x1f\x7f]/.test(s[end])){valid=false;break;}
            else value+=s[end++];
          }
        }
        while(space(s[end]))end++;
        if(valid&&s[end]===')'){
          const next=resolveURL(value);i=end+1;out+=next===value?s.slice(start,i):'url('+quoted(next)+')';
          if(frame)frame.first=false;importURL=false;continue;
        }
        // Leave invalid syntax to the native CSS parser, without inventing URLs.
      }
      out+=s.slice(start,i);importURL=false;
      if(s[i]==='('){out+='(';i++;if(frame)frame.first=false;frames.push({image:lower==='image-set'||lower==='-webkit-image-set',first:true});}
      else if(frame)frame.first=false;
      continue;
    }
    out+=ch;i++;importURL=false;
    if(ch==='(')frames.push({image:false,first:true});
    else if(ch===')')frames.pop();
    else if(ch===','&&frame)frame.first=true;
    else if(frame)frame.first=false;
  }
  return out;
}

if(typeof module!=='undefined'&&module.exports)module.exports={rewriteCSS};

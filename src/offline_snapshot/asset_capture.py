"""Discover declared static resources during capture; never used by packaging."""

from pathlib import Path

from .resource_policy import link_kind

CSS_URLS = Path(__file__).with_name("css_urls.js").read_text(encoding="utf-8")


async def required_assets(frame):
    """Return executable/style resources the saved DOM still requires.

    This inventory is deliberately separate from optional HTTP asset
    collection. Browsers do not report every failed request consistently, so a
    declared script or active stylesheet must also be reconciled against the
    finished response archive.
    """
    return await frame.evaluate("""() => {
      const out=[];
      const add=(url,kind)=>{if(url&&/^https?:/.test(url))out.push({url,kind,frame:location.href})};
      for(const script of document.querySelectorAll('script[src]'))add(script.src,'script');
      for(const link of document.querySelectorAll('link[href]')){
        if(link.relList.contains('stylesheet')&&!link.disabled&&matchMedia(link.media||'all').matches)
          add(link.href,'stylesheet');
      }
      return out;
    }""")


async def declared_assets(frame):
    result = await frame.evaluate(
        "() => {"
        + CSS_URLS
        + """
      const out=[];
      const unreadable=[];
      const add=(url,kind,reason)=>{if(url&&/^https?:/.test(url))out.push({url,kind,reason})};
      for(const image of document.images)add(image.currentSrc||image.src,'image','img selected source or declared src');
      for(const media of document.querySelectorAll('audio,video')){
        if(media.getAttribute('src'))add(media.src,'media','declared '+media.tagName.toLowerCase()+' src');
        for(const source of media.querySelectorAll('source[src]'))add(source.src,'media','declared media alternative');
        if(media.poster)add(media.poster,'image','video poster');
      }
      for(const link of document.querySelectorAll('link[href]'))out.push({url:link.href,kind:'link',rel:link.rel,reason:'declared link asset'});
      const visited=new Set();
      function rules(list,base){
        for(const rule of list){
          if(rule.type===CSSRule.FONT_FACE_RULE){
            rewriteCSS(rule.style.getPropertyValue('src'),value=>{
              try{add(new URL(value,base).href,'font','declared @font-face source')}catch{}
              return value;
            });
          }
          if(rule.styleSheet)sheet(rule.styleSheet,base);
          else if(rule.cssRules)rules(rule.cssRules,base);
        }
      }
      function sheet(value,base){
        if(visited.has(value))return;visited.add(value);
        const source=value.href||base;
        let list;
        try{list=value.cssRules}catch(error){unreadable.push({url:source,reason:String(error)});return;}
        rules(list,source);
      }
      function root(value){
        for(const s of [...(value.styleSheets||[]),...(value.adoptedStyleSheets||[])])sheet(s,document.baseURI);
        for(const element of value.querySelectorAll('*'))if(element.shadowRoot)root(element.shadowRoot);
      }
      root(document);
      return {assets:out,unreadableStylesheets:unreadable};
    }"""
    )
    result["assets"] = [
        c for c in result["assets"] if c["kind"] != "link" or link_kind(c.get("rel")) == "asset"
    ]
    return result


def static_mime(kind, mime):
    mime = mime.lower().split(";")[0].strip()
    if mime == "application/octet-stream":
        return True
    if kind == "image":
        return mime.startswith("image/")
    if kind == "media":
        return mime.startswith(("audio/", "video/"))
    if kind == "font":
        return mime.startswith("font/") or mime in (
            "application/font-woff",
            "application/vnd.ms-fontobject",
            "application/x-font-ttf",
            "application/x-font-opentype",
        )
    return mime.startswith(("image/", "font/")) or mime == "text/css"


async def warm_scroll(page, steps):
    original = await page.evaluate("({x:scrollX,y:scrollY})")
    performed = 0
    try:
        for _ in range(steps):
            before = await page.evaluate("scrollY")
            await page.evaluate("scrollBy(0,Math.max(1,Math.floor(innerHeight*0.85)))")
            await page.wait_for_timeout(250)
            after = await page.evaluate("scrollY")
            if after == before:
                break
            performed += 1
    finally:
        await page.evaluate("p=>scrollTo(p.x,p.y)", original)
        await page.wait_for_timeout(250)
    return {
        "scope": "main document",
        "limit": steps,
        "performed": performed,
        "restoredPosition": original,
    }

"""Record real CSS mutations, stop the server, then inspect standalone replay pixels."""

import argparse
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import unquote, urlsplit

from PIL import Image
from playwright.async_api import async_playwright

from offline_snapshot.archive import load
from offline_snapshot.capture import capture, replay_page
from offline_snapshot.pack import pack

CASES = {
    "property": "tile.style.backgroundImage=css;",
    "set-property": "tile.style.setProperty('--picture',css);tile.style.setProperty('background-image','var(--picture)','important');",
    "css-text": "tile.style.cssText='background-image:'+css;",
    "style-forward": "tile.style='background-image:'+css;",
    "prototype-call": "CSSStyleDeclaration.prototype.setProperty.call(tile.style,'background-image',css);",
    "attribute": "tile.setAttribute('style','background-image:'+css);",
    "markup": "document.querySelector('#slot').innerHTML=`<div id=tile style='background-image:${css}'></div>`;",
    "style-text": "style.textContent='#tile{background-image:'+css+'}';",
    "style-html": "style.innerHTML='#tile{background-image:'+css+'}';",
    "style-node": "style.replaceChildren(document.createTextNode('#tile{background-image:'+css+'}'));",
    "style-data": "if(!style.firstChild)style.appendChild(document.createTextNode(''));style.firstChild.data='#tile{background-image:'+css+'}';",
    "insert-rule": "while(style.sheet.cssRules.length)style.sheet.deleteRule(0);style.sheet.insertRule('@media all {}',0);style.sheet.cssRules[0].insertRule('#tile{background-image:'+css+'}',0);",
    "rule-property": "if(!style.sheet.cssRules.length)style.sheet.insertRule('#tile{}',0);style.sheet.cssRules[0].style.backgroundImage=css;",
    "constructed": "await sheet.replace('#tile{background-image:'+css+'}');document.adoptedStyleSheets=[sheet];",
    "constructed-sync": "sheet.replaceSync('#tile{background-image:'+css+'}');document.adoptedStyleSheets=[sheet];",
    "import": "style.textContent='@import \"'+name+'.css\";';",
    "sheet-relative": "const linked=document.querySelector('#linked').sheet;if(!linked.cssRules.length)linked.insertRule('#tile{}',0);linked.cssRules[0].style.setProperty('background-image','url('+name+'.svg)');",
    "image-set": "tile.style.backgroundImage='image-set(\"'+name+'.svg\" 1x, url(\"'+name+'.svg\") 2x)';",
    "preload-image": "const hint=document.createElement('link');hint.rel='preload';hint.as='image';hint.imageSrcset=new URL(name+'.svg',document.baseURI).href+' 1x';document.head.append(hint);tile.style.backgroundColor=name==='green'?'rgb(20, 160, 70)':'rgb(30, 90, 210)';",
    "missing-css": "tile.style.backgroundImage='url(missing.svg)';",
    "excluded-css": "tile.style.backgroundImage='url(excluded.svg)';",
}


def document(case, origin):
    head = '<base href="/assets/"><link id="linked" rel="stylesheet" href="/theme/main.css">'
    if case == "preload-image":
        head += f'<link rel="preload" as="image" imagesrcset="{origin}/assets/green.svg 1x">'
    return (
        """<!doctype html><html><head><title>Dynamic CSS """
        + case
        + """</title>"""
        + head
        + """<style>
body{margin:24px;font:20px sans-serif}#tile{width:500px;height:220px;background-color:rgb(240, 20, 80)}button{padding:12px}#status{margin:16px 0}
</style></head><body><h1>Dynamic CSS """
        + case
        + """</h1><button id="apply">Apply</button><p id="status">Ready</p><div id="slot"><div id="tile"></div></div><script>
const style=document.createElement('style');document.head.append(style);
const sheet=new CSSStyleSheet();let applied=0;
document.querySelector('#apply').onclick=async()=>{
const name=applied++%2?'blue':'green',css='url("'+name+'.svg")',tile=document.querySelector('#tile');
"""
        + CASES[case]
        + """
document.querySelector('#status').textContent=name;
};</script></body></html>"""
    ).encode()


def svg(name):
    color = {"green": "rgb(20, 160, 70)", "blue": "rgb(30, 90, 210)"}[name]
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="{color}"/></svg>'.encode()


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        body = b""
        mime = "text/plain"
        status = 200
        if path.startswith("/case/"):
            body = document(path.rsplit("/", 1)[1], f"http://127.0.0.1:{self.server.server_port}")
            mime = "text/html"
        elif path.endswith(("green.svg", "blue.svg")):
            body = svg(path.rsplit("/", 1)[1][:-4])
            mime = "image/svg+xml"
        elif path == "/theme/main.css":
            body = b"/* stylesheet-relative mutation */"
            mime = "text/css"
        elif path.endswith(("green.css", "blue.css")):
            body = ("#tile{background-image:url(" + path.rsplit("/", 1)[1][:-4] + ".svg)}").encode()
            mime = "text/css"
        else:
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def main(args):
    out = Path("outputs/dynamic-css")
    out.mkdir(exist_ok=True)
    reports = Path("reports/dynamic-css") / args.phase
    reports.mkdir(parents=True, exist_ok=True)
    cases = args.case or list(CASES)
    if args.capture:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            recipe = out / "recipe.json"
            recipe.write_text(
                json.dumps(
                    {
                        "ready": "#apply",
                        "steps": [
                            {
                                "action": "click",
                                "selector": "#apply",
                                "wait_for": '#status:text-is("green")',
                                "checkpoint": "green",
                                "settle_ms": 300,
                            },
                            {
                                "action": "click",
                                "selector": "#apply",
                                "wait_for": '#status:text-is("blue")',
                                "checkpoint": "blue",
                                "settle_ms": 300,
                            },
                        ],
                    }
                )
            )
            for case in cases:
                exclusions = (
                    [f"http://127.0.0.1:{server.server_port}/assets/excluded.svg"]
                    if case == "excluded-css"
                    else []
                )
                await capture(
                    f"http://127.0.0.1:{server.server_port}/case/{case}",
                    out / f"{case}-{args.phase}.capture.json",
                    recipe,
                    page_wait_ms=100,
                    timeout=10000,
                    exclude_resources=exclusions,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    results = {}
    async with async_playwright() as pw:
        for engine in ["chromium", "firefox"]:
            browser = await getattr(pw, engine).launch(
                **({"chromium_sandbox": True} if engine == "chromium" else {})
            )
            for case in cases:
                archive = load(out / f"{case}-{args.from_phase or args.phase}.capture.json")
                html = out / f"{case}-{args.phase}.html"
                pack(archive, html)
                context = await browser.new_context(
                    offline=True,
                    viewport={"width": 1363, "height": 936},
                    locale="en-US",
                    timezone_id="UTC",
                    service_workers="block",
                )
                result = {
                    "browser": browser.version,
                    "file": str(html),
                    "transport": "file://",
                    "offline": True,
                    "harnessFulfilsResources": False,
                    "errors": [],
                    "networkAttempts": [],
                    "states": [],
                }

                async def block(route, current=result):
                    if route.request.url.startswith(("http:", "https:")):
                        current["networkAttempts"].append(route.request.url)
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route("**/*", block)
                page = await context.new_page()
                page.set_default_timeout(4000)
                page.on(
                    "pageerror",
                    lambda error, current=result: current["errors"].append(str(error)[:700]),
                )
                page.on(
                    "console",
                    lambda message, current=result: (
                        current["errors"].append(message.text[:700])
                        if message.type == "error"
                        else None
                    ),
                )
                try:
                    await page.goto(html.resolve().as_uri())
                    active = await replay_page(page)
                    for name, color in [("green", (20, 160, 70)), ("blue", (30, 90, 210))]:
                        if case in ["missing-css", "excluded-css"]:
                            color = (240, 20, 80)
                        await active.locator("#apply").click()
                        await active.locator('#status:text-is("' + name + '")').wait_for()
                        await page.wait_for_timeout(350)
                        path = reports / f"{engine}-{case}-{name}.png"
                        await active.locator("#tile").screenshot(path=str(path))
                        with Image.open(path).convert("RGB") as im:
                            pixel = im.getpixel((im.width // 2, im.height // 2))
                        result["states"].append(
                            {
                                "name": name,
                                "pixel": pixel,
                                "expected": color,
                                "passed": pixel == color,
                                "css": await active.locator("#tile").evaluate(
                                    "(e)=>getComputedStyle(e).backgroundImage"
                                ),
                            }
                        )
                    result["runtime"] = json.loads(
                        await active.locator("#offline-snapshot-status pre").text_content()
                    )
                    runtime = result["runtime"]
                    valid = not any(runtime.get(k) for k in ["misses", "errors", "violations"])
                    if case == "missing-css":
                        valid = (
                            bool(runtime["misses"])
                            and all(
                                m.get("kind") == "css" and m["url"].endswith("/missing.svg")
                                for m in runtime["misses"]
                            )
                            and not runtime["errors"]
                            and not runtime["violations"]
                        )
                    if case == "excluded-css":
                        valid = valid and any(
                            b.get("kind") == "explicit resource exclusion"
                            and b["url"].endswith("/excluded.svg")
                            for b in runtime["blocked"]
                        )
                    result["expectedUnavailable"] = case in ["missing-css", "excluded-css"]
                    result["passed"] = (
                        all(s["passed"] for s in result["states"])
                        and not result["errors"]
                        and not result["networkAttempts"]
                        and valid
                    )
                except Exception as exc:
                    result["passed"] = False
                    result["failure"] = str(exc)[:1500]
                (reports / f"{engine}-{case}.json").write_text(json.dumps(result, indent=2))
                results[engine + "-" + case] = result["passed"]
                await context.close()
            await browser.close()
    (reports / "summary.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return all(results.values())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--phase", required=True)
    p.add_argument("--capture", action="store_true")
    p.add_argument("--from-phase")
    p.add_argument("--case", action="append", choices=CASES)
    args = p.parse_args()
    raise SystemExit(0 if asyncio.run(main(args)) else 1)

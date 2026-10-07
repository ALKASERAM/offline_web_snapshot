"""Site-independent capture/replay regressions. Server exists only during capture."""

import argparse
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.async_api import async_playwright

from offline_snapshot.archive import load
from offline_snapshot.capture import capture, replay_page
from offline_snapshot.pack import pack
from offline_snapshot.visual import compare

STYLE = "body{margin:40px;background:#eef2f7;color:#16324c;font:18px sans-serif}main{padding:28px;background:white;border-radius:12px;max-width:760px}button,a{margin:10px;padding:8px}img{width:240px;height:100px}iframe{width:700px;height:260px;border:1px solid #345}"


def doc(title, body, scripts=""):
    return f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title><style>{STYLE}</style>{scripts}</head><body><main><h1>{title}</h1>{body}</main></body></html>'.encode()


ROUTER = """const browserWindow=window;
document.querySelector('#ready').textContent='Router ready '+document.querySelector('base').getAttribute('href');
function render(){document.querySelector('#route').textContent=browserWindow.location.pathname+location.search;}
document.querySelector('#next').onclick=()=>{history.pushState({step:2},'', '/routing/details?view=full');render()};
document.querySelector('#replace').onclick=()=>{history.replaceState({step:3},'', '/routing/details?view=compact');render()};
addEventListener('popstate',render);render();"""
PAGES = {
    "/modules": doc(
        "Module graph",
        '<p id="ready">Loading</p><img id="art"><button id="lazy">Load detail</button><p id="detail"></p>',
        '<script type="importmap">{"imports":{"palette":"/js/default-palette.js","kit/":"/js/"},"scopes":{"/js/":{"palette":"/js/palette.js"}}}</script><script type="module" src="/js/main.js"></script>',
    ),
    "/js/main.js": b"""import {color} from 'palette'; import {cycle} from 'kit/cycle-a.js';
document.querySelector('#ready').textContent='Modules ready: '+color+' '+cycle();
document.querySelector('#art').src=new URL('../art.svg',import.meta.url);
document.querySelector('#lazy').onclick=async()=>{const path='./detail.js';const m=await import(path);document.querySelector('#detail').textContent=await m.detail()};""",
    "/js/default-palette.js": b'export const color="red";',
    "/js/palette.js": b'export const color="blue";',
    "/js/cycle-a.js": b'import {b} from "./cycle-b.js";export const a="A";export function cycle(){return a+b()}',
    "/js/cycle-b.js": b'import {a} from "./cycle-a.js";export function b(){return "B"+a}',
    "/js/detail.js": b'export async function detail(){return (await (await fetch("/api")).json()).value}',
    "/routing": doc(
        "History routing",
        '<p id="route"></p><button id="next">Details</button><button id="replace">Compact</button><p id="ready">Router ready</p>',
        '<base href="/">',
    ).replace(b"</body>", ("<script>" + ROUTER + "</script></body>").encode()),
    "/script-base": doc(
        "Dynamic script base",
        '<p id="ready">Loading</p><input id="query" autofocus aria-label="Query">',
        '<script defer src="/js/loader.js"></script>',
    ),
    "/js/loader.js": b'const script=document.createElement("script");script.src=new URL("./loaded.js",document.currentScript.src);document.head.appendChild(script);',
    "/js/loaded.js": b'const anchor=document.createElement("a");anchor.setAttribute("href","/art.svg");if(anchor.href===new URL("/art.svg",location.href).href&&anchor.protocol===location.protocol)document.querySelector("#ready").textContent="Dynamic script ready";',
    "/script-stack": doc(
        "Script stack identity",
        '<p id="ready">Loading</p>',
        '<script defer src="/js/stack.js"></script>',
    ),
    "/js/stack.js": (
        'const capturedData="'
        + "x" * 300000
        + '";const trace=new Error("Recorded stack").stack;document.querySelector("#ready").textContent=trace.includes("/js/stack.js")&&trace.length<20000?"Readable script stack":"Oversized or unidentified stack";'
    ).encode(),
    "/frames": doc(
        "Embedded frame",
        '<p id="ready">Frame host ready</p><div id="frame-slot"></div><iframe id="static-widget" src="/widget" style="display:none"></iframe>',
    ).replace(
        b"</body>",
        b"""<script>document.querySelector('#frame-slot').innerHTML='<iframe id="widget" src="/widget"></iframe>';const blank=document.createElement('iframe');blank.id='blank';blank.style.display='none';blank.src='about:blank';document.body.append(blank);blank.contentDocument.body.textContent='Blank frame ready';</script></body>""",
    ),
    "/widget": doc(
        "Frame widget",
        '<button id="increment">Increment</button><b id="count">0</b><p id="api"></p>',
        '<script defer src="/widget.js"></script>',
    ).replace(b"<!doctype html>", b"<!DOCTYPE>"),
    "/widget.js": b"fetch('/api').then(r=>r.json()).then(x=>document.querySelector('#api').textContent=x.value);document.querySelector('#increment').onclick=()=>document.querySelector('#count').textContent=String(+document.querySelector('#count').textContent+1)",
    "/responsive": doc(
        "Responsive image",
        '<p id="ready">Image ready</p><picture><source media="(min-width: 800px)" srcset="/art.svg 1x, /art.svg?large=1 2x"><img src="/wrong.svg" alt="Recorded artwork"></picture>',
    ),
    "/responsive-live": doc(
        "Dynamic responsive image", '<p id="ready">Loading</p><div id="art"></div>'
    ).replace(
        b"</body>",
        b"""<script>document.querySelector('#art').innerHTML='<picture><source media="(min-width: 800px)" srcset="/art.svg 1x, /art.svg?large=1 2x"><img src="/wrong.svg" alt="Recorded artwork"></picture>';document.querySelector('img').onload=()=>document.querySelector('#ready').textContent='Image ready';</script></body>""",
    ),
    "/storage": doc("Browser state", '<p id="ready">Loading</p><p id="cache"></p>').replace(
        b"</body>",
        b"""<script>
    document.cookie='theme=blue; Path=/';document.querySelector('#ready').textContent=document.cookie;
    caches.open('demo').then(async cache=>{await cache.put('/debugCategories',new Response('Stored response'));document.querySelector('#cache').textContent=await (await cache.match('/debugCategories')).text()});
    </script></body>""",
    ),
    "/workers": doc("Worker replay", '<p id="ready">Loading</p>').replace(
        b"</body>",
        b"""<script>
    const worker=new Worker('/js/worker.js');worker.onmessage=e=>{document.querySelector('#ready').textContent='Worker bytes: '+[...new Uint8Array(e.data)].join(',');worker.terminate()};worker.postMessage('load');
    </script></body>""",
    ),
    "/quirks": doc("Original layout mode", '<p id="ready">Loading</p>')
    .replace(b"<!doctype html>", b"")
    .replace(
        b"</body>",
        b'<script>document.querySelector("#ready").textContent=document.compatMode;</script></body>',
    ),
    "/native-events": doc("Window events", '<p id="ready">Loading</p>').replace(
        b"</body>",
        b"""<script>
    let count=0;const listener=()=>count++;const add=EventTarget.prototype.addEventListener,remove=EventTarget.prototype.removeEventListener;
    add.call(window,'recorded-event',listener);EventTarget.prototype.dispatchEvent.call(window,new Event('recorded-event'));
    remove.call(window,'recorded-event',listener);window.dispatchEvent(new Event('recorded-event'));
    document.querySelector('#ready').textContent='Native events: '+count;
    </script></body>""",
    ),
    "/resource-policy": doc("Explicit resource policy", '<p id="ready">Loading</p>').replace(
        b"</body>",
        b"""<script>
    const xhrBlocked=new Promise(resolve=>{const xhr=new XMLHttpRequest();xhr.open('POST','/telemetry?xhr='+Date.now());xhr.onerror=()=>resolve(xhr.status===0);xhr.onload=()=>resolve(false);xhr.send('event')});
    const fetchBlocked=fetch('/telemetry?fetch='+Date.now(),{method:'POST',body:'event'}).then(()=>false,()=>true);
    const workerBlocked=new Promise(resolve=>{const worker=new Worker('/js/policy-worker.js');worker.onmessage=e=>{resolve(e.data==='blocked');worker.terminate()};worker.postMessage('start')});
    Promise.all([xhrBlocked,fetchBlocked,workerBlocked]).then(async results=>{const value=await (await fetch('/api')).json();document.querySelector('#ready').textContent=results.filter(Boolean).length+' blocked; '+value.value});
    </script></body>""",
    ),
    "/js/policy-worker.js": b"self.onmessage=()=>fetch('/telemetry?worker='+Date.now(),{method:'POST',body:'worker event'}).then(()=>postMessage('unexpected success'),()=>postMessage('blocked'));",
    "/js/worker.js": b"""importScripts('./worker-helper.js');self.onmessage=async()=>{const data=await (await fetch('/binary')).arrayBuffer();postMessage(data,[data]);}""",
    "/js/worker-helper.js": b"self.helperLoaded=true;",
    "/binary": bytes([0, 255, 128, 1]),
    "/api": b'{"value":"Recorded response"}',
    "/art.svg": b'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="100"><rect width="240" height="100" fill="#277ab5"/><circle cx="120" cy="50" r="35" fill="#ffcf63"/></svg>',
    "/wrong.svg": b'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="100"><rect width="240" height="100" fill="red"/></svg>',
}


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        key = self.path.split("?")[0]
        if key.startswith("/routing/"):
            key = "/routing"
        body = PAGES.get(key, b"Not found")
        self.send_response(200 if key in PAGES else 404)
        mime = (
            "application/javascript"
            if key.endswith(".js")
            else "image/svg+xml"
            if key.endswith(".svg")
            else "application/json"
            if key == "/api"
            else "application/octet-stream"
            if key == "/binary"
            else "text/html"
        )
        self.send_header("Content-Type", mime)
        if key == "/storage":
            self.send_header("Set-Cookie", "serverLocale=en; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


RECIPES = {
    "modules": {
        "ready": '#ready:has-text("Modules ready: blue ABA")',
        "images": "#art",
        "steps": [
            {
                "action": "click",
                "selector": "#lazy",
                "wait_for": '#detail:has-text("Recorded response")',
                "checkpoint": "lazy",
            }
        ],
    },
    "routing": {
        "ready": '#ready:has-text("Router ready /")',
        "steps": [
            {
                "action": "click",
                "selector": "#next",
                "wait_for": '#route:has-text("/routing/details?view=full")',
                "checkpoint": "details",
            },
            {
                "action": "click",
                "selector": "#replace",
                "wait_for": '#route:has-text("/routing/details?view=compact")',
                "checkpoint": "compact",
            },
        ],
    },
    "script-base": {"ready": '#ready:has-text("Dynamic script ready")'},
    "script-stack": {"ready": '#ready:has-text("Readable script stack")'},
    "frames": {"ready": "#ready"},
    "responsive": {"ready": "#ready", "images": "img"},
    "responsive-live": {"ready": '#ready:has-text("Image ready")', "images": "img"},
    "storage": {"ready": '#cache:has-text("Stored response")', "expect_text": "theme=blue"},
    "workers": {"ready": '#ready:has-text("Worker bytes: 0,255,128,1")'},
    "quirks": {"ready": '#ready:has-text("BackCompat")'},
    "native-events": {"ready": '#ready:has-text("Native events: 1")'},
    "resource-policy": {"ready": '#ready:has-text("3 blocked; Recorded response")'},
}


async def main(phase, recapture):
    out = Path("outputs/general-engine")
    out.mkdir(parents=True, exist_ok=True)
    report = Path("reports/general-engine") / phase
    report.mkdir(parents=True, exist_ok=True)
    if recapture:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for case, recipe in RECIPES.items():
                recipe_file = out / (case + "-recipe.json")
                recipe_file.write_text(json.dumps(recipe))
                exclusions = (
                    [f"http://127.0.0.1:{server.server_port}/telemetry*"]
                    if case == "resource-policy"
                    else []
                )
                await capture(
                    f"http://127.0.0.1:{server.server_port}/{case}",
                    out / (case + ".capture.json"),
                    recipe_file,
                    page_wait_ms=200,
                    timeout=10000,
                    exclude_resources=exclusions,
                )
                recorded = load(out / (case + ".capture.json"))
                assert recorded["snapshots"][0]["name"] == "initial", (
                    "Cookie names must not overwrite checkpoint labels"
                )
            async with async_playwright() as pw:
                for engine in ("chromium", "firefox"):
                    browser = await getattr(pw, engine).launch(
                        **({"chromium_sandbox": True} if engine == "chromium" else {})
                    )
                    for case in RECIPES:
                        context = await browser.new_context(
                            viewport={"width": 1363, "height": 936},
                            locale="en-US",
                            timezone_id="UTC",
                            service_workers="block",
                        )
                        if case == "resource-policy":
                            await context.route("**/telemetry*", lambda route: route.abort())
                        page = await context.new_page()
                        await page.goto(f"http://127.0.0.1:{server.server_port}/{case}")
                        await page.locator(RECIPES[case]["ready"]).wait_for()
                        if case == "frames":
                            await (
                                page.frame_locator("#widget")
                                .locator('#api:has-text("Recorded response")')
                                .wait_for()
                            )
                        await page.wait_for_function(
                            "[...document.images].every(i=>i.complete&&i.naturalWidth>0)"
                        )
                        await page.screenshot(path=str(out / f"online-{engine}-{case}.png"))
                        await context.close()
                    await browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    for case in RECIPES:
        result = pack(load(out / (case + ".capture.json")), out / (case + "-" + phase + ".html"))
        (report / (case + "-pack.json")).write_text(json.dumps(result, indent=2))
    summary = {}
    async with async_playwright() as pw:
        for engine in ("chromium", "firefox"):
            browser = await getattr(pw, engine).launch(
                **({"chromium_sandbox": True} if engine == "chromium" else {})
            )
            for case in RECIPES:
                context = await browser.new_context(
                    offline=True,
                    viewport={"width": 1363, "height": 936},
                    locale="en-US",
                    timezone_id="UTC",
                    service_workers="block",
                )
                result = {
                    "browser": browser.version,
                    "transport": "file://",
                    "offline": True,
                    "harnessFulfilsResources": False,
                    "errors": [],
                    "networkAttempts": [],
                    "checks": [],
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
                    lambda error, current=result: current["errors"].append(str(error)[:1000]),
                )
                page.on(
                    "console",
                    lambda message, current=result: (
                        current["errors"].append(message.text[:1000])
                        if message.type == "error"
                        else None
                    ),
                )

                async def checkpoint(
                    name, current_page=page, current_engine=engine, current_case=case
                ):
                    await current_page.screenshot(
                        path=str(report / f"{current_engine}-{current_case}-{name}.png"),
                        style="#offline-snapshot-status{visibility:hidden!important}",
                    )

                try:
                    await page.goto((out / (case + "-" + phase + ".html")).resolve().as_uri())
                    active = await replay_page(page)
                    await active.locator(RECIPES[case]["ready"]).wait_for()
                    if case == "modules":
                        await active.wait_for_function(
                            'document.querySelector("#art").naturalWidth>0'
                        )
                    if case == "frames":
                        frame = active.frame_locator("#widget")
                        await frame.locator('#api:has-text("Recorded response")').wait_for()
                        assert (
                            await frame.locator("body").evaluate("()=>document.compatMode")
                            == "BackCompat"
                        )
                        assert (
                            await active.frame_locator("#blank").locator("body").text_content()
                            == "Blank frame ready"
                        )
                        await (
                            active.frame_locator("#static-widget")
                            .locator('#api:has-text("Recorded response")')
                            .wait_for(state="attached")
                        )
                    if case == "responsive":
                        await active.wait_for_function(
                            'document.querySelector("img").naturalWidth>0'
                        )
                    if case == "storage":
                        await active.locator('#ready:has-text("theme=blue")').wait_for()
                    if case == "script-base":
                        await active.wait_for_function('document.activeElement.id==="query"')
                    await checkpoint("initial")
                    reference = out / f"online-{engine}-{case}.png"
                    if reference.exists():
                        result["visual"] = compare(
                            reference,
                            report / f"{engine}-{case}-initial.png",
                            report / f"{engine}-{case}-diff.png",
                        )
                        result["visual"]["excluded"] = (
                            "Only the offline diagnostic panel; screenshot style hides it without changing page layout"
                        )
                    result["checks"].append("initial")
                    if case == "modules":
                        await active.locator("#lazy").click()
                        await active.locator('#detail:has-text("Recorded response")').wait_for()
                        result["checks"].append("dynamic-import-and-api")
                    if case == "routing":
                        await active.locator('#route:has-text("/routing")').wait_for()
                        await active.locator("#next").click()
                        await active.locator(
                            '#route:has-text("/routing/details?view=full")'
                        ).wait_for()
                        await active.locator("#replace").click()
                        await active.locator(
                            '#route:has-text("/routing/details?view=compact")'
                        ).wait_for()
                        result["checks"].append("push-and-replace-state")
                        await page.go_back()
                        await active.locator('#route:text-is("/routing")').wait_for()
                        await page.go_forward()
                        await active.locator(
                            '#route:text-is("/routing/details?view=compact")'
                        ).wait_for()
                        await page.reload()
                        active = await replay_page(page)
                        await active.locator(
                            '#route:text-is("/routing/details?view=compact")'
                        ).wait_for()
                        result["checks"].append("back-forward-reload")
                    if case == "frames":
                        await frame.locator("#increment").click()
                        await frame.locator('#count:text-is("1")').wait_for()
                        result["checks"].append("frame-script-and-api")
                    await checkpoint("final")
                    result["runtime"] = await active.locator(
                        "#offline-snapshot-status pre"
                    ).text_content()
                    stats = json.loads(result["runtime"])
                    result["runtime"] = stats
                    frame_stats = []
                    for child in page.frames:
                        if child == active:
                            continue
                        report_node = child.locator("#offline-snapshot-status pre")
                        if await report_node.count():
                            frame_stats.append(json.loads(await report_node.text_content()))
                    result["frameReports"] = frame_stats
                    unexpected_blocked = [
                        b
                        for r in [stats, *frame_stats]
                        for b in r.get("blocked", [])
                        if case != "resource-policy" or b["kind"] != "explicit resource exclusion"
                    ]
                    result["passed"] = (
                        not result["errors"]
                        and not result["networkAttempts"]
                        and not unexpected_blocked
                        and not any(
                            r.get(k)
                            for r in [stats, *frame_stats]
                            for k in ("misses", "errors", "violations")
                        )
                        and result.get("visual", {}).get("passed", True)
                    )
                except Exception as exc:
                    result["failure"] = str(exc)[:2500]
                    result["passed"] = False
                    await checkpoint("failure")
                (report / f"{engine}-{case}.json").write_text(json.dumps(result, indent=2))
                summary[f"{engine}-{case}"] = result["passed"]
                await context.close()
            await browser.close()
    (report / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return all(summary.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", default="after")
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args.phase, args.capture)) else 1)

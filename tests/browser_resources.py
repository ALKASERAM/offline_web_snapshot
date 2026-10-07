"""Resource semantics regressions: controlled capture, then real offline file replay."""

import argparse
import asyncio
import io
import json
import math
import struct
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from offline_snapshot.archive import load
from offline_snapshot.capture import capture, replay_page
from offline_snapshot.pack import pack

STYLE = "body{font:18px sans-serif;margin:30px;background:#eef2f7}main{background:white;padding:25px;max-width:760px}img{width:240px;height:100px}button{padding:10px}"


def page(title, body, script="", head=""):
    return f'<!doctype html><html><head><title>{title}</title><style>{STYLE}</style>{head}</head><body><main><h1>{title}</h1>{body}<p id="status">Ready</p></main><script>{script}</script></body></html>'.encode()


def svg(color):
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="100"><rect width="240" height="100" fill="{color}"/></svg>'.encode()


def tone(frequency):
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(
            b"".join(
                struct.pack("<h", int(5000 * math.sin(2 * math.pi * frequency * i / 8000)))
                for i in range(4000)
            )
        )
    return out.getvalue()


PAGES = {
    "/scroll-state": page(
        "Preserve interaction state",
        '<button id="open">Open menu</button><div id="menu" hidden>Menu open</div><div style="height:5000px"></div>',
        """
    document.querySelector('#open').onclick=()=>document.querySelector('#menu').hidden=false;
    window.addEventListener('scroll',()=>document.querySelector('#menu').hidden=true);
    """,
    ),
    "/collection": page(
        "Bounded asset collection",
        '<audio preload="none"><source src="/tone-a.wav"><source src="/tone-b.wav"><source src="/redirect.wav"><source src="/partial.wav"></audio>',
    ),
    "/metadata": page(
        "Metadata links",
        '<p id="sample">Stylesheet applied</p><p id="toggle-sample">Toggle stylesheet</p><button id="toggle">Toggle link</button>',
        """
    const canonical=document.createElement('link');canonical.href='/canonical-dynamic';canonical.rel='canonical';canonical.id='dynamic';document.head.append(canonical);
    document.head.insertAdjacentHTML('beforeend','<link id="inserted" rel="alternate" hreflang="fr" href="/french">');
    const stylesheet=document.createElement('link');stylesheet.href='/theme.css';stylesheet.relList.add('stylesheet');document.head.append(stylesheet);
    document.querySelector('#toggle').onclick=()=>{const link=document.querySelector('#toggle-link');link.rel=link.rel==='stylesheet'?'canonical':'stylesheet'};
    """,
        '<link id="static" rel="canonical" href="/canonical-static"><link rel="alternate" hreflang="de" href="/german"><link id="toggle-link" rel="stylesheet" href="/toggle.css">',
    ),
    "/responsive-properties": page(
        "Responsive image properties",
        '<div id="slot"></div>',
        """
    const picture=document.createElement('picture'),source=document.createElement('source'),img=new Image();
    source.srcset='/small.svg 1x, /large.svg 2x';picture.append(source);img.src='/fallback.svg';img.srcset='/small.svg 1x, /large.svg 2x';img.onload=()=>document.querySelector('#status').textContent='Image loaded';picture.append(img);document.querySelector('#slot').append(picture);
    """,
    ),
    "/xhr": page(
        "Binary requests",
        '<button id="send">Send recorded bytes</button>',
        """
    document.querySelector('#send').onclick=async()=>{
      const result=[];
      for(const body of [new Uint8Array([9,0,255,128,8]).subarray(1,4),new DataView(new Uint8Array([9,0,255,128,8]).buffer,1,3),new Blob([new Uint8Array([0,255,128])])]){
        result.push(await new Promise((resolve,reject)=>{const x=new XMLHttpRequest();x.open('POST','/binary');x.onload=()=>resolve(x.responseText);x.onerror=reject;x.send(body)}));
      }
      document.querySelector('#status').textContent=result.join(' | ');
    };
    """,
    ),
    "/media": page(
        "Declared media alternatives",
        '<audio id="sound" preload="none"><source src="/tone-a.wav" type="audio/wav"><source src="/tone-b.wav" type="audio/wav"></audio><button id="load">Load second recording</button>',
        """
    document.querySelector('#load').onclick=()=>{const audio=document.querySelector('audio');audio.onloadeddata=()=>{audio.currentTime=0.1;document.querySelector('#status').textContent='Audio loaded'};audio.src='/tone-b.wav';audio.load()};
    """,
    ),
    "/lazy": page(
        "Lazy resources",
        '<p>Scroll to reveal images</p><div style="height:5000px"></div><img id="native" loading="lazy" src="/native.svg"><img id="observed" alt="Deferred image">',
        """
    const img=document.querySelector('#observed'),observer=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting)){img.src='/observed.svg';observer.disconnect()}});observer.observe(img);
    """,
    ),
    "/small.svg": svg("#277ab5"),
    "/large.svg": svg("#339933"),
    "/fallback.svg": svg("#cc3333"),
    "/native.svg": svg("#5577aa"),
    "/observed.svg": svg("#ffcc33"),
    "/tone-a.wav": tone(440),
    "/tone-b.wav": tone(660),
    "/theme.css": b"#sample{color:rgb(0, 0, 255)}",
    "/toggle.css": b"#toggle-sample{color:rgb(0, 128, 0)}",
}
REQUESTS = []


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        key = urlsplit(self.path).path
        REQUESTS.append(key)
        body = PAGES.get(key, b"Not found")
        if key == "/redirect.wav":
            self.send_response(302)
            self.send_header("Location", "/excluded.wav")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if key == "/partial.wav":
            self.send_response(206)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Range", "bytes 0-3/100")
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"RIFF")
            return
        self.send_response(200 if key in PAGES else 404)
        self.send_header(
            "Content-Type",
            "image/svg+xml"
            if key.endswith(".svg")
            else "audio/wav"
            if key.endswith(".wav")
            else "text/css"
            if key.endswith(".css")
            else "text/html",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        reply = ("bytes:" + ",".join(map(str, body))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


RECIPES = {
    name: {"ready": "#status", "settle_ms": 300}
    for name in ["metadata", "responsive-properties", "xhr", "media", "lazy", "scroll-state"]
}
RECIPES["xhr"]["steps"] = [
    {
        "action": "click",
        "selector": "#send",
        "wait_for": '#status:has-text("bytes:0,255,128 | bytes:0,255,128 | bytes:0,255,128")',
        "checkpoint": "binary",
    }
]
RECIPES["scroll-state"]["ready"] = "#open"
RECIPES["scroll-state"]["steps"] = [
    {"action": "click", "selector": "#open", "wait_for": "#menu", "checkpoint": "menu-open"}
]


async def main(phase, enhanced, recapture, from_phase=None):
    out = Path("outputs/resource-handling")
    out.mkdir(exist_ok=True)
    reports = Path("reports/resource-handling") / phase
    reports.mkdir(parents=True, exist_ok=True)
    if recapture:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            if enhanced:
                recipe_path = out / "collection-recipe.json"
                recipe_path.write_text(json.dumps({"ready": "#status"}))
                REQUESTS.clear()
                await capture(
                    f"http://127.0.0.1:{server.server_port}/collection",
                    out / "collection-limit.capture.json",
                    recipe_path,
                    page_wait_ms=50,
                    asset_limit=1,
                )
                limited = load(out / "collection-limit.capture.json")
                assert limited["assetCollection"]["limitReached"] and "/tone-b.wav" not in REQUESTS
                REQUESTS.clear()
                await capture(
                    f"http://127.0.0.1:{server.server_port}/collection",
                    out / "collection-policy.capture.json",
                    recipe_path,
                    page_wait_ms=50,
                    exclude_resources=[f"http://127.0.0.1:{server.server_port}/excluded.wav"],
                )
                policy = load(out / "collection-policy.capture.json")
                assert "/excluded.wav" not in REQUESTS, "Redirect bypassed resource exclusion"
                assert any(
                    r["status"] == "excluded" and r["url"].endswith("/redirect.wav")
                    for r in policy["assetCollection"]["resources"]
                )
                assert any(
                    r["status"] == "failed" and r["url"].endswith("/partial.wav")
                    for r in policy["assetCollection"]["resources"]
                )
                assert not any(r["url"].endswith("/partial.wav") for r in policy["entries"])
                (reports / "capture-policy.json").write_text(
                    json.dumps(
                        {"countLimit": True, "excludedRedirect": True, "partialBodyRejected": True},
                        indent=2,
                    )
                )
            for case, recipe in RECIPES.items():
                path = out / (case + "-recipe.json")
                path.write_text(json.dumps(recipe))
                options = (
                    {"scroll_steps": 8} if enhanced and case in ["lazy", "scroll-state"] else {}
                )
                await capture(
                    f"http://127.0.0.1:{server.server_port}/{case}",
                    out / (case + "-" + phase + ".capture.json"),
                    path,
                    page_wait_ms=200,
                    timeout=10000,
                    **options,
                )
                if enhanced and case == "scroll-state":
                    from lxml import html as dom

                    archive = load(out / (case + "-" + phase + ".capture.json"))
                    assert (
                        "hidden"
                        not in dom.fromstring(archive["snapshots"][-1]["html"])
                        .get_element_by_id("menu")
                        .attrib
                    ), "Automatic scrolling closed the recorded menu"
                    assert [x["checkpoint"] for x in archive["scrollActions"]] == ["initial"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    summaries = {}
    async with async_playwright() as pw:
        for engine in ["chromium", "firefox"]:
            browser = await getattr(pw, engine).launch(
                **({"chromium_sandbox": True} if engine == "chromium" else {})
            )
            for case in RECIPES:
                archive = load(out / (case + "-" + (from_phase or phase) + ".capture.json"))
                html = out / (case + "-" + phase + ".html")
                pack(archive, html)
                result = {
                    "browser": browser.version,
                    "file": str(html),
                    "transport": "file://",
                    "offline": True,
                    "harnessFulfilsResources": False,
                    "errors": [],
                    "networkAttempts": [],
                }
                context = await browser.new_context(
                    offline=True,
                    viewport={"width": 1363, "height": 936},
                    locale="en-US",
                    timezone_id="UTC",
                    service_workers="block",
                )

                async def block(route, current=result):
                    if route.request.url.startswith(("http:", "https:")):
                        current["networkAttempts"].append(route.request.url)
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route("**/*", block)
                p = await context.new_page()
                p.set_default_timeout(4000)
                p.on(
                    "pageerror",
                    lambda error, current=result: current["errors"].append(str(error)[:500]),
                )
                p.on(
                    "console",
                    lambda message, current=result: (
                        current["errors"].append(message.text[:500])
                        if message.type == "error"
                        else None
                    ),
                )
                try:
                    await p.goto(html.resolve().as_uri())
                    active = await replay_page(p)
                    await active.locator("#status").wait_for()
                    if case == "metadata":
                        for id, expected in [
                            ("static", "/canonical-static"),
                            ("dynamic", "/canonical-dynamic"),
                            ("inserted", "/french"),
                        ]:
                            assert (
                                await active.locator("#" + id).evaluate(
                                    "(e)=>new URL(e.href).pathname"
                                )
                                == expected
                            ), "Metadata URL changed"
                        await active.wait_for_function(
                            'getComputedStyle(document.querySelector("#sample")).color==="rgb(0, 0, 255)"'
                        )
                        await active.locator("#toggle").click()
                        await active.wait_for_function(
                            'getComputedStyle(document.querySelector("#toggle-sample")).color!=="rgb(0, 128, 0)"'
                        )
                        await active.locator("#toggle").click()
                        await active.wait_for_function(
                            'getComputedStyle(document.querySelector("#toggle-sample")).color==="rgb(0, 128, 0)"'
                        )
                    elif case == "responsive-properties":
                        await active.locator('#status:has-text("Image loaded")').wait_for()
                        assert await active.locator("img").evaluate(
                            "(e)=>e.complete&&e.naturalWidth===240"
                        )
                    elif case == "xhr":
                        await active.locator("#send").click()
                        await active.locator(
                            '#status:has-text("bytes:0,255,128 | bytes:0,255,128 | bytes:0,255,128")'
                        ).wait_for()
                    elif case == "media":
                        await active.locator("#load").click()
                        await active.locator('#status:has-text("Audio loaded")').wait_for()
                        await active.wait_for_function(
                            'document.querySelector("audio").duration>0.4&&document.querySelector("audio").currentTime>0'
                        )
                    elif case == "lazy":
                        await active.locator("#observed").scroll_into_view_if_needed()
                        await active.wait_for_function(
                            "[...document.images].every(i=>i.complete&&i.naturalWidth===240)"
                        )
                    elif case == "scroll-state":
                        await active.locator("#open").click()
                        await active.locator("#menu").wait_for(state="visible")
                    await p.wait_for_timeout(250)
                    result["runtime"] = json.loads(
                        await active.locator("#offline-snapshot-status pre").text_content()
                    )
                    result["passed"] = (
                        not result["errors"]
                        and not result["networkAttempts"]
                        and not any(
                            result["runtime"].get(k) for k in ["misses", "errors", "violations"]
                        )
                    )
                except Exception as exc:
                    result["passed"] = False
                    result["failure"] = str(exc)[:1500]
                try:
                    await p.screenshot(path=str(reports / f"{engine}-{case}.png"))
                except Exception as e:
                    result["screenshotError"] = str(e)[:500]
                (reports / f"{engine}-{case}.json").write_text(json.dumps(result, indent=2))
                summaries[engine + "-" + case] = result["passed"]
                await context.close()
            await browser.close()
    (reports / "summary.json").write_text(json.dumps(summaries, indent=2))
    print(json.dumps(summaries, indent=2))
    return all(summaries.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--enhanced", action="store_true")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--from-phase")
    args = parser.parse_args()
    raise SystemExit(
        0 if asyncio.run(main(args.phase, args.enhanced, args.capture, args.from_phase)) else 1
    )

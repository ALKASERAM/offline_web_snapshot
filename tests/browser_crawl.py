"""Capture a controlled online site, stop it, then test the single file offline."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from offline_snapshot.archive import load
from offline_snapshot.capture import capture, replay_page, validate
from offline_snapshot.pack import pack

SCRIPT = b"""fetch('/api').then(r=>r.json()).then(x=>document.querySelector('#api').textContent=x.value);
document.querySelector('#increment').onclick=()=>document.querySelector('#count').textContent=String(+document.querySelector('#count').textContent+1);"""


def document(title, links):
    return f"""<!doctype html><html><head><title>{title}</title><link rel="stylesheet" href="/style.css"><script defer src="/app.js"></script></head><body>
    <h1>{title}</h1><p id="api"></p><img src="/pixel.svg"><button id="increment">Increment</button><b id="count">0</b>{links}</body></html>""".encode()


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        pages = {
            "/start": document(
                "Start",
                '<a id="branch" href="/branch/page">Branch</a><a href="/branch/page#anchor">Duplicate</a><a href="/other">Other</a><a href="/failure">Failure</a><a href="https://example.invalid/">External</a><a href="/logout">Logout</a>',
            ),
            "/branch/page": document(
                "Branch", '<a id="leaf" href="../leaf">Leaf</a><a href="/start">Home</a>'
            ),
            "/other": document("Other", '<a href="/leaf">Shared leaf</a>'),
            "/leaf": document(
                "Leaf",
                '<a id="home" href="/start">Home</a><a id="too-far" href="/third-level">Third level</a>',
            ),
            "/third-level": document("Beyond depth", ""),
            "/app.js": SCRIPT,
            "/api": b'{"value":"Recorded API result"}',
            "/style.css": b"body{font:20px sans-serif;background:#f4f4e8;margin:32px}a{display:block;margin:12px 0}",
            "/pixel.svg": b'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><rect width="24" height="24" fill="green"/></svg>',
        }
        body = pages.get(self.path, b"Failure")
        self.send_response(200 if self.path in pages else 500)
        mime = {
            "/app.js": "application/javascript",
            "/api": "application/json",
            "/style.css": "text/css",
            "/pixel.svg": "image/svg+xml",
        }.get(self.path, "text/html")
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def main():
    report_dir = Path("reports/link-depth")
    report_dir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    start = f"http://127.0.0.1:{server.server_port}/start"
    reports = {}
    try:
        for depth in (0, 1, 2):
            path = Path(f"outputs/crawl-test-depth-{depth}.capture.json")
            reports[str(depth)] = await capture(
                start, path, depth=depth, max_pages=10, page_wait_ms=100, timeout=10000
            )
            pack(load(path), f"outputs/crawl-test-depth-{depth}.html")
        reports["cap"] = await capture(
            start,
            "outputs/crawl-test-cap.capture.json",
            depth=2,
            max_pages=2,
            page_wait_ms=100,
            timeout=10000,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    # From here on, no server is running. The acceptance harness only aborts
    # external requests and loads file://; no resource fulfilment is installed.
    def paths(result):
        return {urlsplit(page["url"]).path for page in result["crawl"]["pages"]}

    assert paths(reports["0"]) == {"/start"}
    assert paths(reports["1"]) == {"/start", "/branch/page", "/other", "/failure"}
    assert paths(reports["2"]) == {"/start", "/branch/page", "/other", "/failure", "/leaf"}
    assert reports["cap"]["crawl"]["limitReached"]
    assert len(reports["cap"]["crawl"]["pages"]) == 2
    assert (
        next(p for p in reports["2"]["crawl"]["pages"] if p["url"].endswith("/failure"))["status"]
        == "failed"
    )
    recipe = {
        "ready": '#api:has-text("Recorded API result")',
        "images": "img",
        "expect_text": "Start",
        "route": "/start",
        "steps": [
            {
                "action": "click",
                "selector": "#branch",
                "wait_for": 'h1:has-text("Branch")',
                "expect_text": "Recorded API result",
                "route": "/branch/page",
                "checkpoint": "branch",
            },
            {
                "action": "click",
                "selector": "#increment",
                "expect_text": "1",
                "assert_selector": "#count",
                "checkpoint": "original-script",
            },
            {
                "action": "click",
                "selector": "#leaf",
                "wait_for": 'h1:has-text("Leaf")',
                "route": "/leaf",
                "images": "img",
                "checkpoint": "leaf",
            },
            {
                "action": "click",
                "selector": "#home",
                "wait_for": 'h1:has-text("Start")',
                "route": "/start",
                "checkpoint": "return",
            },
        ],
    }
    recipe_path = report_dir / "recipe.json"
    recipe_path.write_text(json.dumps(recipe, indent=2))
    reports["browsers"] = {}
    for engine in ("chromium", "firefox"):
        result = await validate(
            "outputs/crawl-test-depth-2.html", engine, recipe_path, report_dir / f"{engine}.json"
        )
        (report_dir / f"{engine}.json").write_text(json.dumps(result, indent=2))
        reports["browsers"][engine] = result["passed"]
    reports["navigation"] = {}
    async with async_playwright() as pw:
        for engine in ("chromium", "firefox"):
            browser = await getattr(pw, engine).launch(
                **({"chromium_sandbox": True} if engine == "chromium" else {})
            )
            context = await browser.new_context(offline=True, service_workers="block")
            attempts = []

            async def block(r, network_attempts=attempts):
                if r.request.url.startswith(("http:", "https:")):
                    network_attempts.append(r.request.url)
                    await r.abort()
                else:
                    await r.continue_()

            await context.route("**/*", block)
            page = await context.new_page()
            await page.goto(Path("outputs/crawl-test-depth-2.html").resolve().as_uri())
            active = await replay_page(page)
            await active.locator("#branch").click()
            await active.locator('h1:has-text("Branch")').wait_for()
            await active.locator("#leaf").click()
            await active.locator('h1:has-text("Leaf")').wait_for()
            await active.locator("#too-far").click()
            await active.locator('[data-message]:has-text("not captured")').wait_for()
            await page.go_back()
            await active.locator('h1:has-text("Branch")').wait_for()
            await page.go_forward()
            await active.locator('h1:has-text("Leaf")').wait_for()
            await page.reload()
            active = await replay_page(page)
            await active.locator('h1:has-text("Leaf")').wait_for()
            await page.screenshot(path=str(report_dir / f"{engine}-history.png"))
            assert attempts == []
            reports["navigation"][engine] = {
                "back": "passed",
                "forward": "passed",
                "reload": "passed",
                "uncapturedLink": "blocked locally",
                "networkAttempts": attempts,
            }
            await browser.close()
    (report_dir / "integration.json").write_text(json.dumps(reports, indent=2))
    print(
        json.dumps(
            {"captureDepths": "passed", "pageLimit": "passed", "browsers": reports["browsers"]},
            indent=2,
        )
    )
    assert all(reports["browsers"].values())


if __name__ == "__main__":
    asyncio.run(main())

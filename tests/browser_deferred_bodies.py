"""Large asynchronous bodies stay compressed until offline code requests them."""

import argparse
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from offline_snapshot.archive import load
from offline_snapshot.capture import capture, replay_page
from offline_snapshot.pack import pack

BODY_BYTES = 8 * 1024 * 1024
FETCHES = 10
EXPECTED_BYTES = BODY_BYTES * (FETCHES + 1)
PAGE = f"""<!doctype html><html><head><meta charset="utf-8"><title>Deferred bodies</title></head>
<body><h1>Deferred response bodies</h1><button id="load">Load recorded data</button>
<p id="status">Ready without API bodies</p><script>
document.querySelector('#load').onclick=async()=>{{
  let total=0;
  for(let index=0;index<{FETCHES};index++){{
    total+=(await (await fetch('/api/'+index)).arrayBuffer()).byteLength;
  }}
  total+=await new Promise((resolve,reject)=>{{
    const xhr=new XMLHttpRequest();xhr.open('GET','/xhr-large');xhr.responseType='arraybuffer';
    xhr.onload=()=>resolve(xhr.response.byteLength);xhr.onerror=reject;xhr.send();
  }});
  const sync=new XMLHttpRequest();sync.open('GET','/sync',false);sync.send();
  document.querySelector('#status').textContent='Loaded '+total+' bytes; '+sync.responseText;
}};
</script></body></html>""".encode()


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            body, mime = PAGE, "text/html; charset=utf-8"
        elif path == "/sync":
            body, mime = b"sync ok", "text/plain"
        elif path == "/xhr-large":
            body, mime = bytes([250]) * BODY_BYTES, "application/octet-stream"
        elif path.startswith("/api/") and path[5:].isdigit() and int(path[5:]) < FETCHES:
            body, mime = bytes([int(path[5:])]) * BODY_BYTES, "application/octet-stream"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def main(phase, recapture):
    outputs = Path("outputs/deferred-bodies")
    outputs.mkdir(parents=True, exist_ok=True)
    reports = Path("reports/deferred-bodies") / phase
    reports.mkdir(parents=True, exist_ok=True)
    archive_path = outputs / f"{phase}.capture.json"
    recipe_path = reports / "recipe.json"
    expected = f"Loaded {EXPECTED_BYTES} bytes; sync ok"
    recipe = {
        "ready": "#load",
        "expect_text": "Ready without API bodies",
        "steps": [
            {
                "action": "click",
                "selector": "#load",
                "wait_for": f'#status:has-text("{expected}")',
                "expect_text": expected,
                "checkpoint": "all-bodies",
            }
        ],
    }
    recipe_path.write_text(json.dumps(recipe, indent=2) + "\n")
    if recapture:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            await capture(
                f"http://127.0.0.1:{server.server_port}/",
                archive_path,
                recipe_path,
                page_wait_ms=50,
                timeout=120000,
                request_timeout=120000,
                asset_limit=0,
                report_dir=reports,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    archive = load(archive_path)
    modes = archive["captureDiagnostics"]["xhrModes"]
    assert modes == {"async": 1, "sync": 1, "unknown": 0, "observations": 2, "truncated": False}, (
        modes
    )
    assert sum(item.get("resourceType") == "fetch" for item in archive["entries"]) == FETCHES
    html_path = outputs / f"{phase}.html"
    packed = pack(archive, html_path)
    assert packed["responseStorage"]["deferredBodies"] == FETCHES + 1, packed["responseStorage"]
    assert packed["responseStorage"]["chunks"] == FETCHES + 1, packed["responseStorage"]
    assert packed["responseStorage"]["eagerBodies"] == 2, packed["responseStorage"]
    results = {}
    async with async_playwright() as playwright:
        for engine in ("chromium", "firefox"):
            launch = {"chromium_sandbox": True} if engine == "chromium" else {}
            browser = await getattr(playwright, engine).launch(**launch)
            context = await browser.new_context(offline=True, service_workers="block")
            attempts = []
            errors = []

            async def block(route, network_attempts=attempts):
                if route.request.url.startswith(("http:", "https:", "ws:", "wss:")):
                    network_attempts.append(route.request.url)
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", block)
            page = await context.new_page()
            page.set_default_timeout(120000)
            page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))
            page.on(
                "console",
                lambda message, captured=errors: (
                    captured.append(message.text) if message.type == "error" else None
                ),
            )
            result = {"browser": browser.version, "networkAttempts": attempts, "errors": errors}
            try:
                await page.goto(html_path.resolve().as_uri())
                active = await replay_page(page)
                await active.locator('#status:text-is("Ready without API bodies")').wait_for()
                before = await page.evaluate("window.__OFFLINE_BODY_STORE__.stats()")
                assert before["loads"] == 0, before
                await active.locator("#load").click()
                await active.locator(f'#status:text-is("{expected}")').wait_for()
                after = await page.evaluate("window.__OFFLINE_BODY_STORE__.stats()")
                runtime = json.loads(
                    await active.locator("#offline-snapshot-status pre").text_content()
                )
                assert after["loads"] == FETCHES + 1, after
                assert after["cachedChunks"] == 1 and after["evictions"] == FETCHES, after
                assert not attempts and not errors, (attempts, errors)
                assert not any(runtime.get(key) for key in ("misses", "errors", "violations")), (
                    runtime
                )
                result.update(passed=True, before=before, after=after, runtime=runtime)
            except Exception as error:
                result.update(passed=False, failure=str(error))
            await page.screenshot(path=str(reports / f"{engine}.png"))
            (reports / f"{engine}.json").write_text(json.dumps(result, indent=2) + "\n")
            results[engine] = result["passed"]
            await context.close()
            await browser.close()
    summary = {
        "passed": all(results.values()),
        "browsers": results,
        "captureBytes": sum(len(item.get("body", "")) for item in archive["entries"]),
        "pack": packed,
    }
    (reports / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    assert summary["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--capture", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.phase, arguments.capture))

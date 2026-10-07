"""Truncated gzip JavaScript: reproduce, recollect while online, replay offline."""

import argparse
import asyncio
import gzip
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from offline_snapshot.archive import load
from offline_snapshot.capture import capture, validate
from offline_snapshot.pack import pack

FULL = b'window.example="' + b"abcdefgh" * 50000 + b'";window.scriptLoaded=true;'
COUNTS = {}


class Source(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        COUNTS[path] = COUNTS.get(path, 0) + 1
        mode = (
            "bad" if path.startswith("/bad") else "changed" if path.startswith("/changed") else "ok"
        )
        if path.endswith(".js"):
            n = COUNTS[path]
            body = gzip.compress(FULL)
            # Browser accepts this truncated gzip stream, and response.body() returns
            # decoded prefix bytes despite status 200 and request.failure == None.
            if n == 2 or mode == "bad":
                body = body[: len(body) // 2]
            tag = '"v2"' if mode == "changed" and n >= 3 else '"v1"'
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Cache-Control", "no-store")
            self.send_header("ETag", tag)
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return
        prefix = "" if mode == "ok" else "/" + mode
        title = "Start" if path.endswith("start") else "Next"
        body = (
            f'<!doctype html><html><head><script src="{prefix}/app.js"></script></head><body><h1 id="ready">{title}</h1><p id="state"></p><a id="next" href="{prefix}/next">Next</a><script>document.querySelector("#state").textContent=window.scriptLoaded?"Script ready":"Script missing"</script></body></html>'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True)
    ap.add_argument("--expect-failure", action="store_true")
    args = ap.parse_args()
    r = Path("reports/script-rewrite") / args.phase
    o = Path("outputs/script-rewrite") / args.phase
    r.mkdir(exist_ok=False)
    o.mkdir(exist_ok=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    items = []
    summary = []
    recipe = r / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "ready": "#ready",
                "expect_text": "Script ready",
                "steps": [
                    {
                        "action": "click",
                        "selector": "#next",
                        "wait_for": 'h1:has-text("Next")',
                        "expect_text": "Script ready",
                        "checkpoint": "next",
                    }
                ],
            }
        )
    )
    try:
        for engine in ["chromium", "firefox"]:
            COUNTS.clear()
            archive = o / (engine + ".capture.json")
            await capture(
                f"http://127.0.0.1:{server.server_port}/start",
                archive,
                depth=1,
                max_pages=2,
                ready_selector="#ready",
                engine=engine,
                timeout=3000,
                page_wait_ms=100,
                asset_limit=0,
                report_dir=r,
            )
            a = load(archive)
            item = {
                "engine": engine,
                "scriptRequests": COUNTS.get("/app.js", 0),
                "integrity": a.get("scriptIntegrity"),
            }
            try:
                pack(a, o / (engine + ".html"))
                item["packed"] = True
            except ValueError as e:
                item.update(packed=False, error=str(e))
            assert item["packed"] != args.expect_failure, item
            if not args.expect_failure:
                assert item["scriptRequests"] == 3, item
                assert (
                    len(a["scriptIntegrity"]["recoveries"]) == 1
                    and not a["scriptIntegrity"]["failures"]
                )
                assert a["warnings"], (
                    "Source state after truncated script must not silently count as complete"
                )
                items.append((engine, o / (engine + ".html")))
            summary.append(item)
            print(engine, item["packed"], flush=True)
        if not args.expect_failure:
            for mode in ["bad", "changed"]:
                COUNTS.clear()
                archive = o / (mode + ".capture.json")
                await capture(
                    f"http://127.0.0.1:{server.server_port}/{mode}/start",
                    archive,
                    depth=1,
                    max_pages=2,
                    ready_selector="#ready",
                    timeout=3000,
                    page_wait_ms=100,
                    asset_limit=0,
                    report_dir=r,
                )
                a = load(archive)
                assert a["scriptIntegrity"]["failures"] and not a["scriptIntegrity"]["recoveries"]
                try:
                    pack(a, o / (mode + ".html"))
                    raise AssertionError("Invalid/changed script must not silently succeed")
                except ValueError as e:
                    assert "Cannot safely rewrite" in str(e)
                summary.append(
                    {
                        "case": mode,
                        "expectedFailure": True,
                        "failures": a["scriptIntegrity"]["failures"],
                    }
                )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    for engine, html in items:
        p = r / (engine + ".json")
        d = await validate(html, engine, recipe, p)
        p.write_text(json.dumps(d, indent=2))
        assert d["passed"], d["checks"]
        summary.append({"offlineEngine": engine, "passed": True})
    (r / "summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

"""Wildcard/tilde query navigation, legacy archive keys and redirect aliases."""

import argparse
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from offline_snapshot.archive import load
from offline_snapshot.capture import capture, validate
from offline_snapshot.pack import pack

QUERY = "q=*&z=~&a=two&a=one"


class Source(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/go":
            self.send_response(302)
            self.send_header("Location", "/target?" + QUERY)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        title = "Start" if path == "/start" else "Target"
        body = (
            f'<!doctype html><html><body><h1>{title}</h1><p id="query"></p><a id="go" href="/go?{QUERY}">Next</a><a id="home" href="/start?{QUERY}">Home</a>'
            + '<script>document.querySelector("#query").textContent=new URL(location.href).searchParams.getAll("a").join(",");</script></body></html>'
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
    ap.add_argument("--from-phase")
    args = ap.parse_args()
    reports = Path("reports/readiness-search") / args.phase
    reports.mkdir(exist_ok=False)
    out = Path("outputs/readiness-search") / args.phase
    out.mkdir(exist_ok=False)
    recipe = reports / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "ready": "#go",
                "ready_timeout_ms": 1000,
                "expect_text": "Start",
                "steps": [
                    {
                        "action": "click",
                        "selector": "#go",
                        "expect_text": "Target",
                        "checkpoint": "target",
                    },
                    {
                        "action": "click",
                        "selector": "#home",
                        "expect_text": "Start",
                        "checkpoint": "return",
                    },
                ],
            }
        )
    )
    if args.from_phase:
        archive = load(Path("outputs/readiness-search") / args.from_phase / "site.capture.json")
    else:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            await capture(
                f"http://127.0.0.1:{server.server_port}/start?" + QUERY,
                out / "site.capture.json",
                depth=1,
                max_pages=3,
                ready_selector="#go",
                page_wait_ms=100,
                report_dir=reports,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        archive = load(out / "site.capture.json")
    pack(archive, out / "site.html")
    results = []
    for engine in ("chromium", "firefox"):
        path = reports / (engine + ".json")
        result = await validate(out / "site.html", engine, recipe, path)
        path.write_text(json.dumps(result, indent=2))
        results.append({"engine": engine, "passed": result["passed"]})
        print(engine, result["passed"], flush=True)
        assert result["passed"] != args.expect_failure
        if result["passed"]:
            assert all("two,one" in c["text"] for c in result["checkpoints"])
    (reports / "summary.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

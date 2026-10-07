"""Chromium/Firefox acceptance for bounded automatic interaction discovery."""

import argparse
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from offline_snapshot.archive import load
from offline_snapshot.workflow import save

PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>Discovery</title></head>
<body><h1>Automatic discovery</h1>
<button id="details" onclick="document.querySelector('#panel').hidden=false;this.setAttribute('aria-expanded','true')">Show details</button>
<section id="panel" hidden>Recorded details</section>
<button id="tab" role="tab" aria-selected="false">Results tab</button>
<section id="result">Waiting</section>
<form><label>Private value <input name="private"></label><button id="submit">Submit form</button></form>
<button id="delete">Delete record</button>
<button id="write">Refresh status</button>
<script>
document.querySelector('#tab').onclick=async event=>{
  event.currentTarget.setAttribute('aria-selected','true');
  document.querySelector('#result').textContent=await (await fetch('/api')).text();
};
document.querySelector('#write').onclick=async()=>fetch('/mutate',{method:'POST',body:'blocked'});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    posts = 0

    def do_GET(self):
        if self.path == "/":
            body, mime = PAGE, "text/html; charset=utf-8"
        elif self.path == "/api":
            body, mime = b"Recorded API result", "text/plain; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).posts += 1
        self.send_error(500, "Automatic discovery must block this request")

    def log_message(self, *args):
        pass


async def main(phase):
    output_dir = Path("outputs/interaction-discovery") / phase
    report_dir = Path("reports/interaction-discovery") / phase
    output_dir.mkdir(parents=True, exist_ok=False)
    report_dir.mkdir(parents=True, exist_ok=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = await save(
            f"http://127.0.0.1:{server.server_port}/",
            output_dir / "site.html",
            report_dir=report_dir,
            browsers=("chromium", "firefox"),
            discover_interactions=True,
            max_actions=10,
            page_wait_ms=50,
            timeout=5000,
            settle_timeout=2500,
            request_timeout=2500,
            asset_limit=0,
            validation_timeout=5000,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert Handler.posts == 0, "Blocked POST reached the source server"
    assert result["passed"], json.dumps(result, indent=2)
    discovery = result["capture"]["interactionDiscovery"]
    assert discovery["status"] == "blocked", discovery
    assert discovery["attempted"] == 3, discovery
    assert discovery["recorded"] == 2, discovery
    assert [step["selector"] for step in discovery["steps"]] == ["#details", "#tab"], discovery
    assert [step["expect_text"] for step in discovery["steps"]] == [
        "Recorded details",
        "Recorded API result",
    ], discovery
    assert any(item["reason"] == "form control" for item in discovery["decisions"]), discovery
    assert any(
        item["reason"] == "potential account or data mutation" for item in discovery["decisions"]
    ), discovery
    assert discovery["blockedRequests"][0]["method"] == "POST", discovery
    archive = load(result["archive"])
    assert [item["name"] for item in archive["snapshots"]] == [
        "initial",
        "discovered-1",
        "discovered-2",
    ]
    checks = []
    for item in result["validation"]:
        report = json.loads(Path(item["report"]).read_text())
        assert report["passed"], report
        assert len(report["checks"]) == 3, report["checks"]
        assert (
            not report["networkAttempts"] and not report["errors"] and not report["consoleErrors"]
        ), report
        checks.append(
            {
                "engine": item["engine"],
                "passed": report["passed"],
                "checkpoints": len(report["checks"]),
            }
        )
    summary = {
        "passed": True,
        "serverPostRequests": Handler.posts,
        "discovery": discovery,
        "validation": checks,
    }
    (report_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.phase))

"""Controlled source-readiness regressions for the general capture pipeline."""

import argparse
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from offline_snapshot.archive import load
from offline_snapshot.capture import capture, validate
from offline_snapshot.pack import pack
from offline_snapshot.readiness import ReadinessError

OVERLAY = '<div id="overlay" style="position:fixed;inset:0;background:white;z-index:10">Loading overlay</div>'


class Source(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        case = self.path.strip("/")
        content = '<!doctype html><html><body><div id="wrapper"><button id="ready" data-clicked="0" onclick="this.dataset.clicked=1">Expected content</button></div>'
        if case in ("overlay", "transient"):
            content += OVERLAY
        if case in ("late", "transient"):
            js = (
                'document.body.insertAdjacentHTML("beforeend",' + json.dumps(OVERLAY) + ")"
                if case == "late"
                else 'document.querySelector("#overlay").remove()'
            )
            content += "<script>setTimeout(()=>{" + js + "},100)</script>"
        if case == "transparent":
            content += "<style>#ready{opacity:0}</style>"
        if case == "ancestor":
            content += "<style>#wrapper{opacity:0}</style>"
        if case == "iframe":
            content = (
                '<!doctype html><html><body><iframe src="/child" style="width:500px;height:300px"></iframe>'
                + OVERLAY
            )
        if case == "disabled":
            content += '<script>document.querySelector("#ready").disabled=true</script>'
        if case == "step":
            content += (
                '<button id="cover">Cover page</button><script>document.querySelector("#cover").onclick=()=>document.body.insertAdjacentHTML("beforeend",'
                + json.dumps(OVERLAY)
                + ");</script>"
            )
        data = (content + "</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    args = parser.parse_args()
    reports = Path("reports/readiness-search") / args.phase
    outputs = Path("outputs/readiness-search") / args.phase
    reports.mkdir(exist_ok=False)
    outputs.mkdir(exist_ok=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    evidence = []
    positives = []
    cases = (
        "clear",
        "transient",
        "overlay",
        "transparent",
        "ancestor",
        "late",
        "iframe",
        "disabled",
        "step",
        "unchecked",
    )
    try:
        for engine in ("chromium", "firefox"):
            for case in cases:
                spec = {
                    "ready": "#ready",
                    "ready_timeout_ms": 1200,
                    "expect_text": "Expected content",
                }
                if case == "iframe":
                    spec["frame"] = "iframe"
                if case == "step":
                    spec["steps"] = [
                        {
                            "action": "click",
                            "selector": "#cover",
                            "ready": "#ready",
                            "ready_timeout_ms": 1200,
                            "checkpoint": "covered",
                        }
                    ]
                if case == "unchecked":
                    spec = {}
                recipe = reports / f"{engine}-{case}-recipe.json"
                recipe.write_text(json.dumps(spec))
                archive = outputs / f"{engine}-{case}.capture.json"
                expected = case in ("clear", "transient", "unchecked")
                item = {"engine": engine, "case": case, "expectedUsable": expected}
                try:
                    result = await capture(
                        f"http://127.0.0.1:{server.server_port}/{case}",
                        archive,
                        recipe,
                        engine=engine,
                        report_dir=reports,
                        timeout=2500,
                        page_wait_ms=300,
                        asset_limit=0,
                    )
                    item["captured"] = True
                    assert expected, item
                    item["sourceChecks"] = result["sourceChecks"]
                    assert result["sourceChecks"][0]["status"] == (
                        "not_run" if case == "unchecked" else "passed"
                    )
                    assert 'data-clicked="1"' not in load(archive)["snapshots"][0]["html"], (
                        "Readiness sent an actual click"
                    )
                    html = outputs / f"{engine}-{case}.html"
                    pack(load(archive), html)
                    positives.append((engine, case, html, recipe, result["screenshots"]))
                except ReadinessError as exc:
                    item.update(captured=False, readiness=exc.evidence)
                    assert not expected, item
                    failure = reports / (archive.stem + "-failure.json")
                    diagnostic = json.loads(failure.read_text())
                    assert Path(diagnostic["screenshot"]).is_file()
                    assert diagnostic["readiness"]["status"] == "failed"
                    assert not archive.exists()
                evidence.append(item)
                (reports / "capture-results.json").write_text(json.dumps(evidence, indent=2))
                print(engine, case, "PASS", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    # No source HTTP server exists during these file:// checks.
    offline = []
    for engine, case, html, recipe, baseline in positives:
        report = reports / f"{engine}-{case}-offline.json"
        result = await validate(html, engine, recipe, report, visual_baseline=baseline)
        report.write_text(json.dumps(result, indent=2))
        assert result["passed"], (engine, case, result["checks"], result["errors"])
        offline.append({"engine": engine, "case": case, "passed": True})
    for engine in ("chromium", "firefox"):
        for case in ("overlay", "transparent", "late"):
            old = Path("outputs/readiness-search/reproduction") / f"{engine}-{case}.html"
            report = reports / f"{engine}-{case}-old-replay.json"
            recipe = reports / f"{engine}-{case}-recipe.json"
            result = await validate(old, engine, recipe, report)
            report.write_text(json.dumps(result, indent=2))
            assert not result["passed"] and result["checks"][0]["readiness"]["status"] == "failed"
            assert Path(result["checkpoints"][0]["screenshot"]).is_file()
            offline.append({"engine": engine, "case": case, "expectedFailure": True})
    (reports / "summary.json").write_text(
        json.dumps({"passed": True, "captures": evidence, "offline": offline}, indent=2)
    )
    print(
        "PASS: 20 capture cases; six positive file replays; six previously false-passing files now rejected.",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())

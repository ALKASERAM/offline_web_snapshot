"""A stalled async subresource must retain depth-zero capture with warnings."""

import argparse
import asyncio
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit

from offline_snapshot.capture import validate

STOP = Event()


class Source(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            mime = "text/html"
            if path == "/hang.js":
                STOP.wait(60)
                body, mime = b"window.backgroundLoaded=true;", "application/javascript"
            elif path == "/slow.js":
                STOP.wait(0.8)
                body, mime = (
                    b'document.querySelector("#state").textContent="Loaded";',
                    "application/javascript",
                )
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            else:
                source = (
                    "/slow.js"
                    if path == "/slow"
                    else "http://127.0.0.1:1/unreachable.js"
                    if path == "/failed-resource"
                    else "/hang.js"
                )
                attrs = "" if path == "/parser-blocked" else " async"
                script = (
                    f'<script{attrs} src="{source}"></script>'
                    if path != "/dynamic-stalled"
                    else '<script>const background=document.createElement("script");background.src="/hang.js";document.head.append(background);</script>'
                )
                body = (
                    f"<!doctype html><html><head><title>Depth zero test</title>"
                    f"{script}</head><body>"
                    '<h1>Visible content</h1><p id="state">Waiting</p><button id="action">Show details</button>'
                    '<p id="result"></p><script>document.querySelector("#action").onclick=()=>'
                    'document.querySelector("#result").textContent="Recorded action works";</script></body></html>'
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--before", action="store_true")
    parser.add_argument("--python", default=sys.executable, help="Optional installed-wheel Python")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    reports = root / "reports/depth-zero" / args.phase
    output = root / "outputs/depth-zero" / args.phase
    reports.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    results, replay = [], []
    try:
        for engine in ("chromium", "firefox"):
            for case in (
                ("stalled",)
                if args.before
                else (
                    "stalled",
                    "dynamic-stalled",
                    "failed-resource",
                    "slow",
                    "parser-blocked",
                    "explicit-ready",
                )
            ):
                name = engine + "-" + case
                html = output / (name + ".html")
                archive = html.with_suffix(".capture.json")
                report_dir = reports / name
                command = [
                    args.python,
                    "-m",
                    "offline_snapshot",
                    "save",
                    f"http://127.0.0.1:{server.server_port}/{case}",
                    "--browser",
                    engine,
                    "--validation-browsers",
                    engine,
                    "--timeout-ms",
                    "1500",
                    "--settle-timeout-ms",
                    "2200" if case == "slow" else "700",
                    "--request-timeout-ms",
                    "700",
                    "--page-wait-ms",
                    "0",
                    "--asset-limit",
                    "0",
                    "-o",
                    str(html),
                    "--report-dir",
                    str(report_dir),
                ]
                if case == "explicit-ready":
                    command += ["--ready-selector", "#missing"]
                start = asyncio.get_running_loop().time()
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd="/tmp",
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), 30)
                (reports / (name + ".stdout")).write_bytes(stdout)
                (reports / (name + ".stderr")).write_bytes(stderr)
                result = json.loads((report_dir / "result.json").read_text())
                item = {
                    "engine": engine,
                    "case": case,
                    "command": command,
                    "exitCode": proc.returncode,
                    "seconds": round(asyncio.get_running_loop().time() - start, 3),
                    "status": result["status"],
                }
                results.append(item)
                (reports / "summary.json").write_text(json.dumps(results, indent=2))
                if args.before:
                    assert proc.returncode == 2 and "Page.wait_for_function" in result["error"], (
                        result
                    )
                    assert not archive.exists() and not html.exists()
                elif case == "parser-blocked":
                    assert proc.returncode == 2 and "Page.goto" in result["error"], result
                    assert not archive.exists() and not html.exists()
                elif case == "explicit-ready":
                    assert proc.returncode == 1 and result["stage"] == "source_readiness", result
                    assert not archive.exists() and not html.exists()
                else:
                    assert archive.exists() and html.exists()
                    a = json.loads(archive.read_text())
                    assert a["crawl"]["depth"] == 0 and len(a["pages"]) == 1
                    assert a["sourceChecks"][0]["status"] == "not_run"
                    if case in ("stalled", "dynamic-stalled"):
                        assert (
                            proc.returncode == 1
                            and result["status"] == "failed"
                            and not result["captureComplete"]
                        ), result
                        assert any("did not settle" in w for w in a["warnings"])
                        assert any(
                            q["url"].endswith("/hang.js")
                            for t in a["captureDiagnostics"]["timeouts"]
                            for q in t["pending"]
                        )
                        assert not any(e["url"].endswith("/hang.js") for e in a["entries"])
                    elif case == "failed-resource":
                        assert (
                            proc.returncode == 1
                            and result["status"] == "failed"
                            and not result["captureComplete"]
                        ), result
                        assert any(
                            "/unreachable.js" in w
                            and (
                                w.startswith("Browser request failed GET ")
                                or w.startswith("Declared script missing from capture: ")
                            )
                            for w in a["warnings"]
                        )
                        assert (
                            any(
                                f["url"].endswith("/unreachable.js")
                                and f["captureImpact"] == "incomplete_capture"
                                for f in a["captureDiagnostics"]["requestFailures"]
                            )
                            or engine == "firefox"
                        )
                        assert any(
                            r["url"].endswith("/unreachable.js") and r["status"] == "missing"
                            for r in a["declaredResourceAudit"]["resources"]
                        )
                        assert not any(e["url"].endswith("/unreachable.js") for e in a["entries"])
                    else:
                        assert (
                            proc.returncode == 0 and result["passed"] and result["captureComplete"]
                        ), result
                        assert not a["warnings"] and "Loaded</p>" in a["snapshots"][0]["html"]
                    replay.append((engine, case, html))
                item["expectedResultConfirmed"] = True
                print(engine, case, proc.returncode, flush=True)
    finally:
        STOP.set()
        server.shutdown()
        server.server_close()
        thread.join()
        (reports / "summary.json").write_text(json.dumps(results, indent=2))
    recipe = reports / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "ready": "#action",
                "expect_text": "Visible content",
                "steps": [
                    {
                        "action": "click",
                        "selector": "#action",
                        "expect_text": "Recorded action works",
                        "checkpoint": "details",
                    }
                ],
            }
        )
    )
    for engine, case, html in replay:
        path = reports / (engine + "-" + case + "-offline.json")
        d = await validate(html, engine, recipe, path)
        path.write_text(json.dumps(d, indent=2))
        misses = d.get("runtimeReport", {}).get("misses", [])
        assert (
            d["interactionsPassed"]
            and not d["networkAttempts"]
            and not d["errors"]
            and not d["consoleErrors"]
        ), d
        if case == "dynamic-stalled":
            assert not d["passed"] and any(m["url"].endswith("/hang.js") for m in misses), d
        elif case == "stalled":
            # Static packing records an omission warning; dynamic insertion
            # records a runtime miss. Both save results must stay incomplete.
            assert any(w.endswith("/hang.js") for w in d["runtimeReport"]["warnings"]), d
            assert d["passed"], d
        else:
            assert d["passed"], d
        results.append(
            {
                "engine": engine,
                "case": case,
                "offlineInteractionChecks": len(d["checks"]),
                "strictPassed": d["passed"],
                "expectedResultConfirmed": True,
            }
        )
    (reports / "summary.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

"""Bounded capture with polling/stalls; real CLI and offline file acceptance."""

import argparse
import asyncio
import json
import os
import signal
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import urlsplit

from offline_snapshot.archive import decode, load
from offline_snapshot.capture import validate
from offline_snapshot.pack import pack

COUNTS = {}
REFERERS = {}
LOCK = Lock()


class Source(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        with LOCK:
            COUNTS[path] = COUNTS.get(path, 0) + 1
            count = COUNTS[path]
        try:
            mime = "text/html"
            delay = 0
            if path in ("/stalled", "/dead-page"):
                time.sleep(12)
                body, mime = b"{}", "application/json"
            elif path in ("/poll", "/body", "/sequence"):
                mime = "application/json"
                delay = (
                    0.8
                    if path == "/poll"
                    else 12
                    if path == "/body"
                    else [0.6, 0.05, 0.1][(count - 1) % 3]
                )
                body = (
                    ['"A"', '"B"', '"A"'][(count - 1) % 3] if path == "/sequence" else "{}"
                ).encode()
            elif path.startswith("/redirect-"):
                time.sleep(0.3)
                hop = int(path.rsplit("-", 1)[1])
                self.send_response(302)
                self.send_header("Location", "/redirect-" + str(hop + 1))
                self.end_headers()
                return
            elif path == "/broken.js":
                REFERERS.setdefault(path, []).append(self.headers.get("Referer", ""))
                time.sleep(0.05)
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            elif path == "/cached.js":
                mime = "application/javascript"
                body = b"window.publicStaticRuns=(window.publicStaticRuns||0)+1"
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            else:
                scripts = {
                    "/polling": "setInterval(()=>fetch('/poll').catch(()=>{}),150)",
                    "/slow": "fetch('/stalled').catch(()=>{})",
                    "/body-page": "fetch('/body').catch(()=>{})",
                    "/heartbeat": "document.querySelector('#ready').hidden=true;setTimeout(()=>document.querySelector('#ready').hidden=false,5600)",
                    "/sequence-page": "(async()=>{const bodies=[];for(let i=0;i<3;i++){const r=await fetch('/sequence');bodies.push(r.json())}document.querySelector('#value').textContent=(await Promise.all(bodies)).join('')})()",
                }
                link = (
                    "/dead-page"
                    if path == "/navigation"
                    else "/static-failure-next"
                    if path == "/static-failure"
                    else "/static-failure-last"
                    if path == "/static-failure-next"
                    else "/static-cache-next"
                    if path == "/static-cache"
                    else "/static-cache-last"
                    if path == "/static-cache-next"
                    else "/next"
                )
                extra = (
                    '<audio preload="none"><source src="/redirect-0"></audio>'
                    if path == "/assets"
                    else ""
                )
                external = (
                    '<script src="/broken.js"></script>'
                    if path.startswith("/static-failure")
                    else '<script src="/cached.js"></script>'
                    if path.startswith("/static-cache")
                    else ""
                )
                body = (
                    f"<!doctype html><html><head><title>Capture progress test</title></head><body>"
                    f'<button id="ready">Ready</button><h1>{"Next" if path == "/next" else "Start"}</h1>'
                    f'<p id="value"></p><a id="next" href="{link}">Next page</a>{extra}'
                    f"{external}<script>{scripts.get(path, '')}</script></body></html>"
                ).encode()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            if path == "/cached.js":
                self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.flush()
            time.sleep(delay)
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--case", action="append")
    parser.add_argument("--browser", choices=["chromium", "firefox", "both"], default="both")
    args = parser.parse_args()
    reports = Path("reports/capture-progress") / args.phase
    output = Path("outputs/capture-progress") / args.phase
    reports.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    summary, offline = [], []

    async def run(engine, case, *, cancel=False):
        COUNTS.clear()
        REFERERS.clear()
        name = engine + "-" + case
        archive = output / (name + ".capture.json")
        command = [
            sys.executable,
            "-m",
            "offline_snapshot",
            "capture",
            f"http://127.0.0.1:{server.server_port}/{case}",
            "-o",
            str(archive),
            "--browser",
            engine,
            "--depth",
            "2" if case in ("static-failure", "static-cache") else "1",
            "--max-pages",
            "3"
            if case in ("static-failure", "static-cache")
            else "2"
            if case in ("sequence-page", "navigation")
            else "1",
            "--ready-selector",
            "#missing" if cancel else "#ready",
            "--timeout-ms",
            "30000" if cancel else "8000" if case in ("heartbeat", "static-failure") else "2000",
            "--settle-timeout-ms",
            "1500" if case == "sequence-page" else "600",
            "--request-timeout-ms",
            "1200" if case in ("polling", "sequence-page") else "450",
            "--page-wait-ms",
            "0",
            "--asset-limit",
            "1" if case == "assets" else "0",
            "--report-dir",
            str(reports),
        ]
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        task = asyncio.create_task(proc.communicate())
        if cancel:
            log = reports / (name + ".capture-progress.jsonl")
            for _ in range(100):
                if log.exists() and "loading page" in log.read_text():
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.3)
            proc.send_signal(signal.SIGINT)
        watchdog = False
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(task), 16)
        except asyncio.TimeoutError:
            watchdog = True
            os.killpg(proc.pid, signal.SIGTERM)
            stdout, stderr = await task
        item = dict(
            engine=engine,
            case=case,
            command=command,
            exitCode=proc.returncode,
            seconds=round(time.monotonic() - started, 3),
            watchdog=watchdog,
            requests=dict(COUNTS),
        )
        summary.append(item)
        (reports / "summary.json").write_text(json.dumps(summary, indent=2))
        (reports / (name + ".stdout")).write_bytes(stdout)
        (reports / (name + ".stderr")).write_bytes(stderr)
        assert not watchdog, item
        events = [
            json.loads(line)
            for line in (reports / (name + ".capture-progress.jsonl")).read_text().splitlines()
        ]
        assert events and all("elapsedSeconds" in e and "pending" in e for e in events)
        assert b"responses," in stderr and b"pages saved" in stderr
        if cancel:
            assert proc.returncode == 130 and not archive.exists(), item
            assert events[-1]["event"] == "cancelled"
            assert (
                json.loads((reports / (name + ".capture-failure.json")).read_text())["status"]
                == "cancelled"
            )
            item["passed"] = True
            return
        result = json.loads(stdout)
        data = load(archive)
        diag = data["captureDiagnostics"]
        assert diag["limits"]["settleTimeoutMs"] == (1500 if case == "sequence-page" else 600)
        if case in ("sequence-page", "heartbeat", "static-cache"):
            assert proc.returncode == 0 and result["captureComplete"], result
            if case == "heartbeat":
                assert any(e["event"] == "heartbeat" for e in events)
            elif case == "static-cache":
                cache = diag["publicStaticCache"]
                assert COUNTS.get("/cached.js") == 1, COUNTS
                assert cache["storedResources"] == 1, cache
                assert cache["fulfilledRequests"] == 2, cache
                assert cache["bytesAvoided"] == 2 * len(
                    b"window.publicStaticRuns=(window.publicStaticRuns||0)+1"
                ), cache
                assert (
                    len([page for page in data["crawl"]["pages"] if page["status"] == "recorded"])
                    == 3
                ), data["crawl"]
                assert (
                    len([entry for entry in data["entries"] if entry["url"].endswith("/cached.js")])
                    == 1
                ), data["entries"]
            else:
                assert [decode(e) for e in data["entries"] if e["url"].endswith("/sequence")] == [
                    b'"A"',
                    b'"B"',
                    b'"A"',
                ]
                assert events[-1]["pagesSaved"] == 2 and events[-1]["visit"] == 2
                html = output / (name + ".html")
                pack(data, html)
                offline.append((engine, html, result["screenshots"]))
        else:
            assert proc.returncode == 1 and not result["captureComplete"] and result["warnings"], (
                result
            )
            if case in ("slow", "polling"):
                assert any(
                    t["operation"] == "request settling" and t["pending"] for t in diag["timeouts"]
                )
                expected = "/stalled" if case == "slow" else "/poll"
                assert any(
                    r["url"].endswith(expected) for t in diag["timeouts"] for r in t["pending"]
                )
            elif case == "body-page":
                assert COUNTS["/body"] == 1, "Never retry an arbitrary API request"
                # Firefox can defer the response event until body bytes arrive.
                # That leaves a browser request, rather than a body reader, at
                # the cutoff. Both must be reported and remain unavailable.
                item["bodyReaderTimedOut"] = any(
                    t["operation"].endswith("/body") for t in diag["timeouts"]
                )
                if engine == "chromium":
                    assert item["bodyReaderTimedOut"]
                assert any(
                    r["url"].endswith("/body") for t in diag["timeouts"] for r in t["pending"]
                )
                assert not any(e["url"].endswith("/body") for e in data["entries"])
            elif case == "navigation":
                assert any(p["status"] == "failed" for p in result["crawl"]["pages"])
                assert item["seconds"] < 11, (
                    "Diagnostic screenshot must not wait for the 12-second source"
                )
            elif case == "assets":
                assert COUNTS.get("/redirect-1") == 1 and "/redirect-2" not in COUNTS
                assert data["assetCollection"]["resources"][0]["status"] == "failed"
            elif case == "static-failure":
                assert COUNTS.get("/broken.js", 0) >= 1, COUNTS
                assert all(
                    urlsplit(value).path == "/static-failure" for value in REFERERS["/broken.js"]
                ), REFERERS
                circuit = diag["staticFailureCircuit"]
                assert circuit["suppressedRequests"] == 2, circuit
                assert circuit["resources"][0]["url"].endswith("/broken.js"), circuit
                assert (
                    len([page for page in data["crawl"]["pages"] if page["status"] == "recorded"])
                    == 3
                ), data["crawl"]
        item["passed"] = True
        print(engine, case, item["seconds"], flush=True)

    try:
        for engine in ("chromium", "firefox") if args.browser == "both" else (args.browser,):
            for case in args.case or (
                "slow",
                "polling",
                "body-page",
                "navigation",
                "assets",
                "heartbeat",
                "sequence-page",
                "static-failure",
                "static-cache",
                "cancel",
            ):
                await run(engine, case, cancel=case == "cancel")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        (reports / "summary.json").write_text(json.dumps(summary, indent=2))
    recipe = reports / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "ready": "#ready",
                "expect_text": "ABA",
                "steps": [
                    {
                        "action": "click",
                        "selector": "#next",
                        "expect_text": "Next",
                        "checkpoint": "crawl-1",
                    }
                ],
            }
        )
    )
    for engine, html, baseline in offline:
        path = reports / (engine + "-offline.json")
        result = await validate(html, engine, recipe, path, visual_baseline=baseline)
        path.write_text(json.dumps(result, indent=2))
        summary.append({"engine": engine, "offlinePassed": result["passed"]})
        (reports / "summary.json").write_text(json.dumps(summary, indent=2))
        assert result["passed"], result["checks"]


if __name__ == "__main__":
    asyncio.run(main())

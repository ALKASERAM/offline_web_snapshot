"""Exercise a real wheel outside the checkout, including file:// replay.

Usage: .venv/bin/python tests/browser_cli.py --wheel path/to/package.whl --phase name
The controlled source server is stopped before the final offline checks.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit

STOP = Event()


class Source(BaseHTTPRequestHandler):
    posts = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        pages = {
            "/": b'<!doctype html><html><head><link rel="stylesheet" href="/style.css"></head><body><h1>Start</h1><button id="toggle">Load details</button><p id="value">Waiting</p><a id="next" href="/next">Next page</a><script src="/app.js"></script></body></html>',
            "/next": b'<!doctype html><html><body><h1>Second page</h1><a id="third" href="/third">Third page</a></body></html>',
            "/third": b'<!doctype html><html><body><h1>Third page</h1><a id="back" href="/">Start page</a></body></html>',
            "/fail": b'<!doctype html><html><body><h1>Unrecorded request test</h1><script>if(window.__OFFLINE_ARCHIVE__)fetch("/missing-response").catch(()=>{});</script></body></html>',
            "/covered": b'<!doctype html><html><body><h1 id="content">Expected content</h1><div style="position:fixed;inset:0;background:white;z-index:99">Overlay</div></body></html>',
            "/polling": b'<!doctype html><html><body><button id="toggle">Ready</button><script>setInterval(()=>fetch("/api"),100)</script></body></html>',
            "/async-stalled": b'<!doctype html><html><head><script async src="/hung.js"></script></head><body><h1>Visible despite stalled script</h1></body></html>',
            "/discovery": b"""<!doctype html><html><body><h1>Discovery</h1>
                <button id="details" onclick="document.querySelector('#panel').hidden=false">Show details</button><p id="panel" hidden>Visible details</p>
                <button id="tab" role="tab">Results tab</button><p id="result">Waiting</p>
                <form><button id="submit">Submit form</button></form><button id="delete">Delete record</button>
                <button id="write">Refresh status</button><script>
                document.querySelector('#tab').onclick=async()=>document.querySelector('#result').textContent=await(await fetch('/discovery-api')).text();
                document.querySelector('#write').onclick=()=>fetch('/mutate',{method:'POST',body:'blocked'});
                </script></body></html>""",
        }
        resources = {
            "/app.js": (
                "application/javascript",
                b'document.querySelector("#toggle").onclick=async()=>{document.querySelector("#value").textContent=await(await fetch("/api")).text();};',
            ),
            "/api": ("text/plain", b"Recorded details"),
            "/style.css": (
                "text/css",
                b"body{background:#eef4f8;color:#203040;font:20px sans-serif;padding:24px}",
            ),
            "/favicon.ico": ("image/x-icon", b""),
            "/discovery-api": ("text/plain", b"Recorded discovery result"),
        }
        path = urlsplit(self.path).path
        if path == "/hung.js":
            STOP.wait(30)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.end_headers()
                self.wfile.write(b"window.backgroundLoaded=true;")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path in pages:
            mime, data = "text/html", pages[path]
        elif path in resources:
            mime, data = resources[path]
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        type(self).posts += 1
        self.send_error(500, "Automatic discovery must block writes")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--phase", default="installed")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = root / "outputs/cli-release" / args.phase
    reports = root / "reports/cli-release" / args.phase
    out.mkdir(parents=True, exist_ok=False)
    reports.mkdir(parents=True, exist_ok=False)
    evidence = []

    def run(name, cmd, expected=0, **kwargs):
        completed = subprocess.run(
            [str(x) for x in cmd],
            capture_output=True,
            text=True,
            **kwargs,
        )
        (reports / f"{name}.stdout").write_text(completed.stdout)
        (reports / f"{name}.stderr").write_text(completed.stderr)
        evidence.append(
            {"name": name, "exitCode": completed.returncode, "expectedExitCode": expected}
        )
        (reports / "commands.json").write_text(json.dumps(evidence, indent=2))
        print(name, completed.returncode, flush=True)
        assert completed.returncode == expected, (
            name,
            completed.stderr[-2500:],
            completed.stdout[-1000:],
        )
        return completed

    recipe = reports / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "ready": "#toggle",
                "expect_text": "Start",
                "settle_ms": 100,
                "steps": [
                    {
                        "action": "click",
                        "selector": "#toggle",
                        "wait_for": '#value:has-text("Recorded details")',
                        "expect_text": "Recorded details",
                        "checkpoint": "details",
                    },
                    {
                        "action": "click",
                        "selector": "#next",
                        "expect_text": "Second page",
                        "checkpoint": "second",
                    },
                    {
                        "action": "click",
                        "selector": "#third",
                        "expect_text": "Third page",
                        "checkpoint": "third",
                    },
                    {
                        "action": "click",
                        "selector": "#back",
                        "expect_text": "Start",
                        "checkpoint": "return",
                    },
                ],
            }
        )
    )
    with tempfile.TemporaryDirectory(prefix="offline-snapshot-wheel-") as td:
        temp = Path(td)
        env = {**os.environ, "OFFLINE_SNAPSHOT_CACHE": str(temp / "tool cache")}
        env.pop("PYTHONPATH", None)
        run("venv", [sys.executable, "-m", "venv", temp / "venv"])
        py = temp / "venv/bin/python"
        cli = temp / "venv/bin/offline-snapshot"
        run(
            "install",
            [py, "-m", "pip", "install", str(Path(args.wheel).resolve()) + "[cli]"],
            cwd=temp,
            env=env,
        )
        installed = json.loads(
            run(
                "import",
                [
                    py,
                    "-c",
                    'import json,offline_snapshot;from offline_snapshot.workflow import save;print(json.dumps({"version":offline_snapshot.__version__,"path":offline_snapshot.__file__,"callable":callable(save)}))',
                ],
                cwd=temp,
                env=env,
            ).stdout
        )
        assert not Path(installed["path"]).is_relative_to(root)
        run("doctor-before", [cli, "doctor"], expected=2, cwd=temp, env=env)
        run("setup", [cli, "setup"], cwd=temp, env=env)
        doctor = json.loads(run("doctor-after", [cli, "doctor"], cwd=temp, env=env).stdout)
        assert doctor["passed"]
        run("version", [cli, "--version"], cwd=temp, env=env)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            saved = json.loads(
                run(
                    "save",
                    [
                        cli,
                        "save",
                        url + "/",
                        "-o",
                        out / "site.html",
                        "--depth",
                        "2",
                        "--recipe",
                        recipe,
                        "--report-dir",
                        reports / "save",
                    ],
                    cwd=temp,
                    env=env,
                ).stdout
            )
            assert saved["passed"] and len(saved["validation"]) == 2
            assert Path(saved["archive"]).is_file()
            archive = json.loads(Path(saved["archive"]).read_text())
            assert {url + "/", url + "/next", url + "/third"} <= {
                p["url"] for p in archive["pages"]
            }
            before = hashlib.sha256((out / "site.html").read_bytes()).hexdigest()
            run(
                "overwrite-refused",
                [cli, "save", url + "/", "-o", out / "site.html", "--report-dir", reports / "save"],
                expected=2,
                cwd=temp,
                env=env,
            )
            assert before == hashlib.sha256((out / "site.html").read_bytes()).hexdigest()
            failed = json.loads(
                run(
                    "save-missing",
                    [
                        cli,
                        "save",
                        url + "/fail",
                        "-o",
                        out / "missing.html",
                        "--report-dir",
                        reports / "missing",
                    ],
                    expected=1,
                    cwd=temp,
                    env=env,
                ).stdout
            )
            assert len(failed["validation"]) == 2 and all(
                not v["passed"] for v in failed["validation"]
            )
            draft = json.loads(
                run(
                    "save-unverified",
                    [
                        cli,
                        "save",
                        url + "/",
                        "-o",
                        out / "unverified.html",
                        "--no-validate",
                        "--report-dir",
                        reports / "unverified",
                    ],
                    cwd=temp,
                    env=env,
                ).stdout
            )
            assert draft["status"] == "not_validated" and not draft["passed"]
            ready = json.loads(
                run(
                    "ready-selector",
                    [
                        cli,
                        "save",
                        url + "/",
                        "-o",
                        out / "ready.html",
                        "--ready-selector",
                        "#toggle",
                        "--settle-timeout-ms",
                        "5000",
                        "--request-timeout-ms",
                        "4000",
                        "--report-dir",
                        reports / "ready",
                    ],
                    cwd=temp,
                    env=env,
                ).stdout
            )
            assert ready["passed"] and ready["capture"]["sourceChecks"][0]["status"] == "passed"
            diagnostics = ready["capture"]["captureDiagnostics"]
            assert diagnostics["limits"]["requestTimeoutMs"] == 4000
            assert Path(diagnostics["progressLog"]).is_file()
            incomplete = json.loads(
                run(
                    "incomplete-capture",
                    [
                        cli,
                        "capture",
                        url + "/polling",
                        "-o",
                        out / "incomplete.capture.json",
                        "--ready-selector",
                        "#toggle",
                        "--settle-timeout-ms",
                        "600",
                        "--report-dir",
                        reports / "incomplete",
                    ],
                    expected=1,
                    cwd=temp,
                    env=env,
                ).stdout
            )
            assert not incomplete["captureComplete"] and Path(incomplete["archive"]).is_file()
            stalled = json.loads(
                run(
                    "depth-zero-stalled",
                    [
                        cli,
                        "save",
                        url + "/async-stalled",
                        "-o",
                        out / "stalled.html",
                        "--timeout-ms",
                        "1500",
                        "--settle-timeout-ms",
                        "700",
                        "--page-wait-ms",
                        "0",
                        "--asset-limit",
                        "0",
                        "--validation-browsers",
                        "chromium",
                        "--report-dir",
                        reports / "stalled",
                    ],
                    expected=1,
                    cwd=temp,
                    env=env,
                ).stdout
            )
            assert stalled["status"] == "failed" and not stalled["captureComplete"]
            assert Path(stalled["archive"]).is_file() and Path(stalled["output"]).is_file()
            assert stalled["capture"]["sourceChecks"][0]["status"] == "not_run"
            ready_archive = json.loads(Path(ready["archive"]).read_text())
            assert not any(e["url"] == url + "/api" for e in ready_archive["entries"]), (
                "Readiness activated the button"
            )
            discovered = json.loads(
                run(
                    "interaction-discovery",
                    [
                        cli,
                        "save",
                        url + "/discovery",
                        "-o",
                        out / "discovery.html",
                        "--discover-interactions",
                        "--max-actions",
                        "10",
                        "--page-wait-ms",
                        "50",
                        "--settle-timeout-ms",
                        "2500",
                        "--request-timeout-ms",
                        "2500",
                        "--report-dir",
                        reports / "discovery",
                    ],
                    cwd=temp,
                    env=env,
                ).stdout
            )
            discovery = discovered["capture"]["interactionDiscovery"]
            assert (
                discovered["passed"]
                and discovery["recorded"] == 2
                and discovery["status"] == "blocked"
            ), discovered
            assert [step["selector"] for step in discovery["steps"]] == ["#details", "#tab"], (
                discovery
            )
            assert Source.posts == 0, "Blocked discovery POST reached the source server"
            discovery_recipe = reports / "discovery-recipe.json"
            discovery_recipe.write_text(json.dumps({"steps": discovery["steps"]}))
            query = json.loads(
                run(
                    "query-url",
                    [
                        cli,
                        "save",
                        url + "/?q=*&z=~",
                        "-o",
                        out / "query.html",
                        "--ready-selector",
                        "#toggle",
                        "--report-dir",
                        reports / "query",
                    ],
                    cwd=temp,
                    env=env,
                ).stdout
            )
            assert query["passed"] and len(query["validation"]) == 2
            run(
                "covered-source",
                [
                    cli,
                    "save",
                    url + "/covered",
                    "-o",
                    out / "covered.html",
                    "--ready-selector",
                    "#content",
                    "--timeout-ms",
                    "1500",
                    "--report-dir",
                    reports / "covered",
                ],
                expected=1,
                cwd=temp,
                env=env,
            )
            covered = json.loads((reports / "covered/result.json").read_text())
            assert covered["status"] == "failed" and covered["stage"] == "source_readiness"
            assert not (out / "covered.html").exists()
            run(
                "capture-error",
                [
                    cli,
                    "save",
                    url + "/404",
                    "-o",
                    out / "404.html",
                    "--report-dir",
                    reports / "404",
                ],
                expected=2,
                cwd=temp,
                env=env,
            )
            error = json.loads((reports / "404/result.json").read_text())
            assert error["status"] == "error" and error["stage"] == "capture"
        finally:
            STOP.set()
            server.shutdown()
            server.server_close()
            thread.join()
        # The source server is gone before these acceptance runs. No resource
        # fulfillment or localhost serving is available to the replay viewer.
        run(
            "offline-both",
            [
                cli,
                "validate",
                out / "site.html",
                "--browser",
                "both",
                "--recipe",
                recipe,
                "-o",
                reports / "offline.json",
            ],
            cwd=temp,
            env=env,
        )
        run(
            "offline-pack",
            [cli, "pack", out / "site.capture.json", "-o", out / "repacked.html"],
            cwd=temp,
            env=env,
        )
        run(
            "offline-repacked",
            [
                cli,
                "validate",
                out / "repacked.html",
                "--browser",
                "both",
                "--recipe",
                recipe,
                "-o",
                reports / "repacked.json",
            ],
            cwd=temp,
            env=env,
        )
        run(
            "offline-discovery",
            [
                cli,
                "validate",
                out / "discovery.html",
                "--browser",
                "both",
                "--recipe",
                discovery_recipe,
                "-o",
                reports / "discovery-offline.json",
            ],
            cwd=temp,
            env=env,
        )
        (reports / "summary.json").write_text(
            json.dumps(
                {
                    "passed": True,
                    "installed": installed,
                    "doctor": doctor,
                    "commands": evidence,
                    "sourceServerStoppedBeforeFinalChecks": True,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()

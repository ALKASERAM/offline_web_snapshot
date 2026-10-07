"""Local blob/data XHR regression, compared with native browser results."""

import argparse
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from offline_snapshot.archive import load
from offline_snapshot.capture import capture, validate
from offline_snapshot.pack import pack

SCRIPT = r"""
window.results={};
const local=URL.createObjectURL(new Blob([new Uint8Array([0,255,128,1])],{type:'application/octet-stream'}));
function xhr(url,options={}){return new Promise(resolve=>{const x=new XMLHttpRequest();x.open('GET',url,options.sync?false:true);if(options.type)x.responseType=options.type;if(options.range)x.setRequestHeader('Range',options.range);x.onload=()=>resolve({status:x.status,data:options.type==='arraybuffer'?[...new Uint8Array(x.response)]:x.responseText});x.onerror=()=>resolve({status:x.status,error:true});x.send();});}
(async()=>{
 results.blob=await xhr(local,{type:'arraybuffer'});
 results.range=await xhr(local,{type:'arraybuffer',range:'bytes=1-2'});
 results.data=await xhr('data:application/json,%7B%22local%22%3Atrue%7D');
 results.sync=await xhr('data:text/plain,synchronous',{sync:true});
 results.fetchBlob=[...new Uint8Array(await (await fetch(local)).arrayBuffer())];
 results.fetchData=await (await fetch('data:text/plain,inline')).text();
 const aborted=new XMLHttpRequest();let loads=0;aborted.open('GET',local);aborted.onload=()=>loads++;aborted.send();aborted.abort();
 results.abort={state:aborted.readyState,status:aborted.status,loads};
 aborted.open('GET','data:text/plain,reopened');aborted.onload=()=>{results.reopen=aborted.responseText;document.querySelector('#result').textContent=JSON.stringify(results)};aborted.send();
})();
"""


class Source(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = (
            '<!doctype html><html><body><pre id="result">Loading</pre><script>'
            + SCRIPT
            + "</script></body></html>"
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
    r = Path("reports/readiness-search") / args.phase
    o = Path("outputs/readiness-search") / args.phase
    r.mkdir(exist_ok=False)
    o.mkdir(exist_ok=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Source)
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    items = []
    try:
        for engine in ("chromium", "firefox"):
            recipe = r / (engine + "-recipe.json")
            recipe.write_text(
                json.dumps({"ready": '#result:has-text("reopened")', "ready_timeout_ms": 2500})
            )
            archive = o / (engine + ".capture.json")
            cap = await capture(
                f"http://127.0.0.1:{server.server_port}/",
                archive,
                recipe,
                engine=engine,
                asset_limit=0,
                report_dir=r,
            )
            pack(load(archive), o / (engine + ".html"))
            items.append((engine, recipe, cap["screenshots"]))
    finally:
        server.shutdown()
        server.server_close()
        t.join()
    results = []
    for engine, recipe, refs in items:
        report = r / (engine + ".json")
        d = await validate(o / (engine + ".html"), engine, recipe, report, visual_baseline=refs)
        report.write_text(json.dumps(d, indent=2))
        results.append(
            {
                "engine": engine,
                "passed": d["passed"],
                "misses": d.get("runtimeReport", {}).get("misses"),
                "violations": d.get("runtimeReport", {}).get("violations"),
            }
        )
        print(engine, d["passed"], flush=True)
        assert d["passed"] != args.expect_failure
    (r / "summary.json").write_text(json.dumps(results, indent=2))


asyncio.run(main())

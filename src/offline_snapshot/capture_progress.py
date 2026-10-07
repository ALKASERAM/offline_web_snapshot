"""Capture progress and durable waiting evidence; no browser/replay dependencies."""

import asyncio
import json
import time
from pathlib import Path


class CaptureProgress:
    def __init__(self, path, url, max_pages, callback=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self.callback = callback
        self.started = time.monotonic()
        self.requests = {}
        self.state = dict(
            phase="starting",
            url=url,
            checkpoint=None,
            visit=1,
            maxPages=max_pages,
            pagesSaved=0,
            responses=0,
            bytes=0,
        )
        self.diagnostics = {
            "timeouts": [],
            "requestFailures": [],
            "pages": [],
            "progressLog": str(self.path),
        }

    def start_request(self, key, url, method, kind, source):
        self.requests[key] = dict(
            url=url, method=method, resourceType=kind, source=source, started=time.monotonic()
        )

    def end_request(self, key):
        self.requests.pop(key, None)

    def snapshot(self):
        now = time.monotonic()
        pending = [
            {k: v for k, v in request.items() if k != "started"}
            | {"ageSeconds": round(now - request["started"], 3)}
            for request in self.requests.values()
        ]
        pending.sort(key=lambda item: item["ageSeconds"], reverse=True)
        return {**self.state, "elapsedSeconds": round(now - self.started, 3), "pending": pending}

    def emit(self, event="progress", **changes):
        self.state.update(changes)
        item = {"event": event, **self.snapshot()}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
        if self.callback:
            # JSON quoting also prevents page-controlled URLs from inserting
            # terminal control sequences or fake progress lines.
            url = json.dumps(item["url"], ensure_ascii=True)
            prefix = "timeout during " if event == "timeout" else ""
            text = (
                f"[{item['elapsedSeconds']:.1f}s] {prefix}{item['phase']} | "
                f"visit {item['visit']}/{item['maxPages']} | {item['pagesSaved']} pages saved | "
                f"{item['responses']} responses, {item['bytes'] / 1024 / 1024:.1f} MiB recorded | "
                f"{len(item['pending'])} pending operations | {url}"
            )
            if item.get("warningCount"):
                text += f" | {item['warningCount']} capture warnings (incomplete)"
            for request in item["pending"][:3]:
                text += (
                    f"\n  waiting {request['ageSeconds']:.1f}s ({request['source']}): "
                    f"{request['method']} {json.dumps(request['url'], ensure_ascii=True)}"
                )
            self.callback(text)
        return item

    def timeout(self, operation, limit_ms):
        evidence = {"operation": operation, "timeoutMs": limit_ms, **self.snapshot()}
        self.diagnostics["timeouts"].append(evidence)
        self.emit("timeout")
        return evidence

    async def heartbeat(self):
        while True:
            await asyncio.sleep(5)
            self.emit("heartbeat")

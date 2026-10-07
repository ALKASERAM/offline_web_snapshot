"""Optional Playwright recorder for a user-controlled capture environment."""

import asyncio
import json
import platform
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .archive import canonical_url, entry, new_archive, privacy_audit, save
from .asset_capture import declared_assets, required_assets, static_mime, warm_scroll
from .capture_progress import CaptureProgress
from .crawl import CrawlPlan, page_url, route
from .discovery import new_visible_text, scan_candidates, state_fingerprint
from .readiness import ReadinessError, check_ready, read_recipe
from .resource_policy import excluded
from .script_integrity import complete_scripts
from .toolchain import browser_launch_options

STATIC_REQUEST_TYPES = frozenset({"script", "stylesheet", "image", "font", "media"})


class StaticFailureCircuit:
    """Avoid repeating an already-failed static request on every crawled page."""

    def __init__(self, diagnostics):
        self.failures = {}
        self.resources = {}
        self.diagnostics = diagnostics.setdefault(
            "staticFailureCircuit",
            {
                "policy": "After one unexcluded GET failure, identical static requests on later crawled pages are aborted locally.",
                "suppressedRequests": 0,
                "resources": [],
            },
        )

    @staticmethod
    def key(method, url, resource_type):
        if str(method).upper() != "GET" or resource_type not in STATIC_REQUEST_TYPES:
            return None
        try:
            return ("GET", canonical_url(url), resource_type)
        except ValueError:
            return None

    def failed(self, method, url, resource_type):
        key = self.key(method, url, resource_type)
        if key is not None:
            self.failures[key] = self.failures.get(key, 0) + 1

    def suppress(self, method, url, resource_type):
        key = self.key(method, url, resource_type)
        if key is None or not self.failures.get(key):
            return False
        item = self.resources.get(key)
        if item is None:
            item = {
                "method": key[0],
                "url": key[1],
                "resourceType": key[2],
                "priorFailures": self.failures[key],
                "suppressedRequests": 0,
            }
            self.resources[key] = item
            self.diagnostics["resources"].append(item)
        item["suppressedRequests"] += 1
        self.diagnostics["suppressedRequests"] += 1
        return True


class PublicStaticResponseCache:
    """Reuse explicitly public static responses while crawling linked pages."""

    def __init__(self, diagnostics, max_bytes=64 * 1024 * 1024):
        self.entries = {}
        self.bytes = 0
        self.max_bytes = max_bytes
        self.diagnostics = diagnostics.setdefault(
            "publicStaticCache",
            {
                "policy": "Only complete 200 GET static responses with explicit public freshness and no request-dependent Vary header are reused.",
                "storedResources": 0,
                "storedBytes": 0,
                "fulfilledRequests": 0,
                "bytesAvoided": 0,
                "capacityBytes": max_bytes,
                "capacitySkips": 0,
                "resources": [],
            },
        )

    @staticmethod
    def key(method, url, resource_type, request_headers=None):
        if str(method).upper() != "GET" or resource_type not in STATIC_REQUEST_TYPES:
            return None
        if (request_headers or {}).get("range"):
            return None
        try:
            return ("GET", canonical_url(url), resource_type)
        except ValueError:
            return None

    @staticmethod
    def public_fresh_seconds(headers):
        headers = {str(key).lower(): str(value) for key, value in headers.items()}
        directives = {}
        for part in headers.get("cache-control", "").split(","):
            name, separator, value = part.strip().partition("=")
            if name:
                directives[name.lower()] = value.strip('"') if separator else None
        if "public" not in directives or {"private", "no-store", "no-cache"} & directives.keys():
            return 0
        try:
            max_age = int(directives.get("max-age", "0"))
        except ValueError:
            return 0
        try:
            age = max(0, int(headers.get("age", "0")))
        except ValueError:
            return 0
        if headers.get("date"):
            try:
                apparent = (
                    datetime.now(timezone.utc) - parsedate_to_datetime(headers["date"])
                ).total_seconds()
                age = max(age, max(0, apparent))
            except (TypeError, ValueError, OverflowError):
                pass
        vary = {item.strip().lower() for item in headers.get("vary", "").split(",") if item.strip()}
        if vary - {"accept-encoding"} or "set-cookie" in headers:
            return 0
        return max(0, max_age - age)

    def store(self, method, url, resource_type, status, request_headers, response_headers, body):
        key = self.key(method, url, resource_type, request_headers)
        if key is None or status != 200 or key in self.entries:
            return
        fresh_seconds = self.public_fresh_seconds(response_headers)
        if not fresh_seconds:
            return
        body = bytes(body)
        if len(body) > 20 * 1024 * 1024 or self.bytes + len(body) > self.max_bytes:
            self.diagnostics["capacitySkips"] += 1
            return
        omitted = {
            "connection",
            "content-encoding",
            "content-length",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "set-cookie",
            "transfer-encoding",
        }
        headers = {
            name: value for name, value in response_headers.items() if name.lower() not in omitted
        }
        report = {"url": key[1], "resourceType": key[2], "bytes": len(body), "fulfilledRequests": 0}
        self.entries[key] = {
            "status": status,
            "headers": headers,
            "body": body,
            "report": report,
            "expiresAt": time.monotonic() + fresh_seconds,
        }
        self.bytes += len(body)
        self.diagnostics["storedResources"] += 1
        self.diagnostics["storedBytes"] += len(body)
        self.diagnostics["resources"].append(report)

    def lookup(self, method, url, resource_type, request_headers=None):
        lowered = {
            str(name).lower(): str(value).lower() for name, value in (request_headers or {}).items()
        }
        request_cache = lowered.get("cache-control", "")
        if (
            "no-cache" in request_cache
            or "no-store" in request_cache
            or re.search(r"(?:^|,)\s*max-age\s*=\s*0(?:\s*,|$)", request_cache)
            or "no-cache" in lowered.get("pragma", "")
        ):
            return None
        key = self.key(method, url, resource_type, request_headers)
        cached = self.entries.get(key) if key is not None else None
        if cached is not None and time.monotonic() >= cached["expiresAt"]:
            self.entries.pop(key, None)
            return None
        return cached

    def fulfilled(self, cached):
        size = len(cached["body"])
        cached["report"]["fulfilledRequests"] += 1
        self.diagnostics["fulfilledRequests"] += 1
        self.diagnostics["bytesAvoided"] += size


def annotate_xhr_modes(entries, observations):
    """Mark responses lazy-safe only when every observed matching XHR was async."""
    counts = {"async": 0, "sync": 0, "unknown": 0}
    for record in entries:
        if record.get("resourceType") != "xhr":
            continue
        try:
            key = (record["method"].upper(), canonical_url(record["url"]))
        except (KeyError, ValueError):
            key = None
        modes = observations.get(key, set()) if key else set()
        mode = "sync" if False in modes else "async" if modes == {True} else "unknown"
        record["xhrMode"] = mode
        counts[mode] += 1
    return counts


async def check_state(page, spec, *, timeout=15000):
    readiness = await check_ready(page, spec, timeout=timeout)
    if spec.get("route"):
        await page.wait_for_function(
            'expected => { const href=window.__offlineLocation?.href || (window.__OFFLINE_ARCHIVE__?.embeddedNavigation ? window.__OFFLINE_ARCHIVE__.url : location.href); const u=new URL(href); return (u.hash.startsWith("#!") ? new URL(u.hash.slice(2),"https://offline.invalid") : u).pathname===expected; }',
            arg=spec["route"],
        )
    if spec.get("images"):
        await page.wait_for_function(
            "selector => {const images = [...document.querySelectorAll(selector)]; return images.length > 0 && images.every(img => img.complete && img.naturalWidth > 0);}",
            arg=spec["images"],
        )
    if spec.get("count_selector"):
        count = await page.locator(spec["count_selector"]).count()
        if count != spec["expected_count"]:
            raise AssertionError(f"Expected {spec['expected_count']} items; found {count}")
    if spec.get("expect_text"):
        actual = await page.locator(spec.get("assert_selector", "body")).inner_text()
        if spec["expect_text"] not in actual:
            raise AssertionError(spec["expect_text"])
    return readiness


async def replay_page(page):
    """A multipage file keeps its active document in an embedded blob frame."""
    container = page.locator("#offline-page")
    if await container.count():
        element = await container.element_handle()
        frame = await element.content_frame()
        await frame.wait_for_function("Boolean(window.__OFFLINE_ARCHIVE__)")
        return frame
    return page


async def recipe_scope(page, spec):
    if not spec.get("frame"):
        return page
    element = await page.locator(spec["frame"]).element_handle()
    frame = await element.content_frame()
    if frame is None:
        raise ValueError("Recipe frame is not loaded")
    return frame


async def perform_action(page, spec):
    target = (await recipe_scope(page, spec)).locator(spec["selector"])
    action = spec["action"]
    if action == "click":
        await target.click()
    elif action == "fill":
        await target.fill(spec["value"])
    elif action == "press":
        await target.press(spec["value"])
    elif action == "wait":
        await target.wait_for(state="visible")
    elif action == "drag":
        await target.scroll_into_view_if_needed()
        bounds = await target.bounding_box()
        if not bounds:
            raise ValueError("Drag target has no visible bounds")
        start = spec.get("from", [0.5, 0.5])
        end = spec["to"]
        if len(start) != 2 or len(end) != 2 or any(not 0 <= v <= 1 for v in [*start, *end]):
            raise ValueError("Drag coordinates must be fractions between 0 and 1")
        mouse = (page.page if hasattr(page, "page") else page).mouse
        await mouse.move(
            bounds["x"] + start[0] * bounds["width"], bounds["y"] + start[1] * bounds["height"]
        )
        await mouse.down()
        try:
            await mouse.move(
                bounds["x"] + end[0] * bounds["width"],
                bounds["y"] + end[1] * bounds["height"],
                steps=20,
            )
        finally:
            await mouse.up()
    else:
        raise ValueError("Unknown recipe action: " + action)


async def capture(
    url,
    output,
    recipe_path=None,
    headed=False,
    timeout=45000,
    depth=0,
    max_pages=25,
    include_paths=(),
    exclude_paths=(),
    page_wait_ms=1000,
    exclude_resources=(),
    engine="chromium",
    asset_limit=200,
    scroll_steps=0,
    capture_cookies=False,
    discover_interactions=False,
    max_actions=25,
    include_actions=(),
    exclude_actions=(),
    *,
    report_dir=None,
    ready_selector=None,
    progress=None,
    settle_timeout=None,
    request_timeout=None,
):
    settle_timeout = timeout if settle_timeout is None else settle_timeout
    request_timeout = timeout if request_timeout is None else request_timeout
    if (
        depth < 0
        or max_pages < 1
        or page_wait_ms < 0
        or asset_limit < 0
        or scroll_steps < 0
        or max_actions < 1
        or min(timeout, settle_timeout, request_timeout) <= 0
    ):
        raise ValueError("Invalid crawl limits")
    if engine not in ("chromium", "firefox"):
        raise ValueError("Unknown capture browser")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Install offline-snapshot[cli], then run offline-snapshot setup"
        ) from exc
    a = new_archive(url)
    # A fresh browser capture starts with empty storage. Record this explicitly
    # so replay never inherits state from the browser opening the snapshot.
    a["storage"] = {"localStorage": {}, "sessionStorage": {}}
    a["privacyPolicy"] = {
        "scriptVisibleCookies": "captured" if capture_cookies else "omitted",
        "note": "Request/response bodies and rendered page content are retained for replay and may still contain sensitive data.",
    }
    a["pages"] = []
    a["resourceExclusions"] = list(exclude_resources)
    a["resourceOmissions"] = []
    a["assetCollection"] = {
        "limit": asset_limit,
        "resources": [],
        "unreadableStylesheets": [],
        "note": "Additional HTTP collection of DOM-declared static assets and readable CSSOM font sources; not browser-response recording. Inaccessible sheets and closed shadow roots are not scanned for fonts.",
    }
    a["declaredResourceAudit"] = {
        "resources": [],
        "note": "Scripts and active stylesheets declared by saved DOM checkpoints are reconciled with the final response archive independently of browser failure events.",
    }
    a["scrollActions"] = []
    a["interactionDiscovery"] = {
        "enabled": bool(discover_interactions),
        "mode": "safe non-form controls",
        "maxActions": max_actions,
        "includeSelectors": list(include_actions),
        "excludeSelectors": list(exclude_actions),
        "status": "pending" if discover_interactions else "disabled",
        "attempted": 0,
        "recorded": 0,
        "decisions": [],
        "steps": [],
        "safety": {
            "forms": "excluded",
            "accountAndMutationLabels": "excluded by conservative heuristic",
            "nonReadRequests": "blocked before reaching the server",
            "topLevelNavigation": "limited by same-origin crawl policy",
            "note": "Discovery is bounded and heuristic; it is not an exhaustive interaction or safety proof.",
        },
    }
    recipe = read_recipe(recipe_path, ready_selector)
    a["sourceChecks"] = []
    pending = set()
    ordered = {}
    next_response = 0
    total = 0
    rendered_links = {}
    reports = Path(report_dir) if report_dir is not None else Path("reports")
    capture_dir = reports / (Path(output).stem + "-screenshots")
    capture_dir.mkdir(parents=True, exist_ok=True)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    monitor = CaptureProgress(
        reports / (Path(output).stem + "-progress.jsonl"), url, max_pages, progress
    )
    a["captureDiagnostics"] = monitor.diagnostics
    monitor.diagnostics["limits"] = {
        "actionTimeoutMs": timeout,
        "settleTimeoutMs": settle_timeout,
        "requestTimeoutMs": request_timeout,
    }
    static_response_cache = PublicStaticResponseCache(monitor.diagnostics)
    cache_fulfilled_requests = set()
    monitor.emit()
    async with async_playwright() as pw:
        launch_options = browser_launch_options(engine)
        browser = await getattr(pw, engine).launch(headless=not headed, **launch_options)
        context = await browser.new_context(
            viewport={"width": 1363, "height": 936},
            locale="en-US",
            timezone_id="UTC",
            service_workers="block",
        )
        xhr_modes = {}
        xhr_mode_events = 0
        xhr_modes_truncated = False

        def record_xhr_mode(source, item):
            nonlocal xhr_mode_events, xhr_modes_truncated
            if not isinstance(item, dict):
                return
            if xhr_mode_events >= 10000:
                xhr_modes_truncated = True
                return
            try:
                key = (str(item["method"]).upper(), canonical_url(str(item["url"])))
                asynchronous = item["async"] is True
            except (KeyError, TypeError, ValueError):
                return
            xhr_mode_events += 1
            xhr_modes.setdefault(key, set()).add(asynchronous)

        await context.expose_binding("__offlineSnapshotRecordXHRMode", record_xhr_mode)
        await context.add_init_script(r"""(() => {
  const nativeOpen=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(method,url,asynchronous=true,...rest){
    try{
      const detail={method:String(method).toUpperCase(),url:new URL(String(url),document.baseURI).href,async:asynchronous!==false};
      Promise.resolve(globalThis.__offlineSnapshotRecordXHRMode(detail)).catch(()=>{});
    }catch{}
    return Reflect.apply(nativeOpen,this,[method,url,asynchronous,...rest]);
  };
})();""")
        # This is a fresh capture context, never a personal profile. Only cookies
        # visible to page scripts are needed for source locale/consent decisions;
        # HttpOnly cookies and Set-Cookie response headers remain excluded.
        if capture_cookies:
            await context.add_init_script(
                "try { Object.defineProperty(window,'__offlineInitialCookie',{value:document.cookie}); } catch {}"
            )

        async def resource_policy(route):
            pattern = excluded(route.request.url, exclude_resources)
            if pattern and not (
                route.request.is_navigation_request() and route.request.frame == page.main_frame
            ):
                a["resourceOmissions"].append(
                    {
                        "url": route.request.url,
                        "pattern": pattern,
                        "reason": "Explicit resource exclusion",
                    }
                )
                await route.abort()
            else:
                await route.continue_()

        if exclude_resources:
            await context.route("**/*", resource_policy)

        async def record(response, sequence):
            nonlocal total
            request = response.request
            key = ("body", sequence)
            monitor.start_request(
                key, response.url, request.method, request.resource_type, "response body"
            )
            try:
                headers = dict(await response.all_headers())
                completed_from_http = False
                try:
                    body = await asyncio.wait_for(response.body(), timeout=request_timeout / 1000)
                except Exception as body_error:
                    if isinstance(body_error, asyncio.TimeoutError):
                        monitor.timeout("response body: " + response.url, request_timeout)
                        body_error = TimeoutError(f"Response body exceeded {request_timeout} ms")
                    # A short-lived Worker can terminate before Playwright reads
                    # its already-consumed body. Complete only an observed,
                    # successful GET of a static/binary resource, and report
                    # this HTTP collection separately from browser recording.
                    mime = headers.get("content-type", "")
                    eligible = (
                        request.method == "GET"
                        and 200 <= response.status < 300
                        and bool(
                            re.search(
                                r"image/|font/|javascript|text/css|octet-stream|application/wasm|protobuf",
                                mime,
                            )
                        )
                    )
                    if not eligible:
                        raise body_error
                    retry_headers = {
                        k: v for k, v in request.headers.items() if k in ("range", "accept")
                    }
                    if headers.get("etag") and not headers["etag"].startswith("W/"):
                        retry_headers["if-match"] = headers["etag"]
                    monitor.start_request(
                        key, response.url, request.method, request.resource_type, "HTTP completion"
                    )
                    completed = await context.request.get(
                        response.url, headers=retry_headers, timeout=request_timeout
                    )
                    try:
                        if completed.status != response.status:
                            raise RuntimeError(
                                "Resource completion changed HTTP status"
                            ) from body_error
                        body = await completed.body()
                        headers = dict(completed.headers)
                        completed_from_http = True
                    finally:
                        await completed.dispose()
                    a["collectionMethod"] = "browser + HTTP completion of observed static resources"
                    a.setdefault("resourceCompletions", []).append(
                        {
                            "url": response.url,
                            "range": retry_headers.get("range"),
                            "reason": str(body_error)[:500],
                        }
                    )
                if len(body) > 20 * 1024 * 1024 or total + len(body) > 150 * 1024 * 1024:
                    a["warnings"].append("Resource budget exceeded: " + response.url)
                    return
                if not completed_from_http:
                    static_response_cache.store(
                        request.method,
                        response.url,
                        request.resource_type,
                        response.status,
                        request.headers,
                        headers,
                        body,
                    )
                # Never persist response cookies in the portable artifact.
                headers = {
                    k: v
                    for k, v in headers.items()
                    if k.lower() not in ("set-cookie", "authorization", "proxy-authorization")
                }
                rec = entry(
                    response.url,
                    body,
                    headers.get("content-type", "application/octet-stream"),
                    response.status,
                    request.method,
                    request.post_data_buffer or b"",
                    headers,
                )
                rec["resourceType"] = request.resource_type
                if request.headers.get("range"):
                    rec["requestHeaders"] = {"range": request.headers["range"]}
                # Preserve A→B→A sequences and response arrival order, even when
                # asynchronous body reads finish in a different order.
                ordered[sequence] = rec
                total += len(body)
                monitor.state.update(responses=len(ordered), bytes=total)
            except Exception as exc:
                a["warnings"].append("Capture failed " + response.url + ": " + str(exc))
                monitor.diagnostics["requestFailures"].append(
                    {
                        "url": response.url,
                        "method": request.method,
                        "operation": "response recording",
                        "error": str(exc),
                    }
                )
            finally:
                monitor.end_request(key)

        def on_response(response):
            nonlocal next_response
            if response.request in cache_fulfilled_requests:
                cache_fulfilled_requests.discard(response.request)
                return
            sequence = next_response
            next_response += 1
            task = asyncio.create_task(record(response, sequence))
            pending.add(task)
            task.add_done_callback(pending.discard)

        context.on("response", on_response)
        page = await context.new_page()
        page.on("worker", lambda worker: a.setdefault("workerURLs", []).append(worker.url))
        page.set_default_timeout(timeout)
        active_requests = set()
        discovery_blocked_requests = set()
        circuit_suppressed_requests = set()
        static_failure_circuit = StaticFailureCircuit(monitor.diagnostics)
        recording_open = True
        last_activity = asyncio.get_running_loop().time()

        def track_start(request):
            nonlocal last_activity
            if not recording_open:
                return
            monitor.start_request(
                request, request.url, request.method, request.resource_type, "browser"
            )
            if request.resource_type in (
                "xhr",
                "fetch",
                "script",
                "stylesheet",
                "image",
                "font",
                "document",
            ):
                active_requests.add(request)
                last_activity = asyncio.get_running_loop().time()

        def track_end(request):
            nonlocal last_activity
            monitor.end_request(request)
            if request in active_requests:
                active_requests.discard(request)
                last_activity = asyncio.get_running_loop().time()

        def track_failure(request):
            failure = request.failure or "Unknown browser request failure"
            if request in circuit_suppressed_requests:
                circuit_suppressed_requests.discard(request)
                track_end(request)
                return
            if request in discovery_blocked_requests:
                monitor.diagnostics["requestFailures"].append(
                    {
                        "url": request.url,
                        "method": request.method,
                        "operation": "automatic interaction discovery",
                        "error": failure,
                        "captureImpact": "intentionally_blocked",
                    }
                )
                track_end(request)
                return
            pattern = excluded(request.url, exclude_resources)
            if request in monitor.requests:
                monitor.diagnostics["requestFailures"].append(
                    {
                        "url": request.url,
                        "method": request.method,
                        "operation": "browser request",
                        "error": failure,
                        "captureImpact": "explicitly_excluded" if pattern else "incomplete_capture",
                    }
                )
            if not pattern:
                static_failure_circuit.failed(request.method, request.url, request.resource_type)
                warning = f"Browser request failed {request.method} {request.url}: {failure}"
                if warning not in a["warnings"]:
                    a["warnings"].append(warning)
            track_end(request)

        context.on("request", track_start)
        context.on("requestfinished", track_end)
        context.on("requestfailed", track_failure)
        heartbeat = asyncio.create_task(monitor.heartbeat())

        async def settle(wait_for_load=False):
            monitor.emit(phase="settling", url=page.url)
            await page.wait_for_timeout(page_wait_ms)
            deadline = asyncio.get_running_loop().time() + settle_timeout / 1000
            while True:
                if not active_requests and asyncio.get_running_loop().time() - last_activity >= 0.5:
                    if (
                        not wait_for_load
                        or await page.evaluate("document.readyState") == "complete"
                    ):
                        return
                if asyncio.get_running_loop().time() >= deadline:
                    evidence = monitor.timeout("request settling", settle_timeout)
                    evidence["settlingRequests"] = [request.url for request in active_requests]
                    a["warnings"].append(
                        f"Page did not settle within {settle_timeout} ms: " + page.url
                    )
                    return
                await page.wait_for_timeout(100)

        collected = set()
        required = {}

        async def audit_declared_resources():
            for frame in page.frames:
                try:
                    for item in await required_assets(frame):
                        required.setdefault((canonical_url(item["url"]), item["kind"]), item)
                except Exception as exc:
                    a["warnings"].append(
                        "Declared resource audit failed for frame "
                        + frame.url
                        + ": "
                        + str(exc)[:200]
                    )

        async def collect_assets():
            nonlocal next_response, total
            if not asset_limit:
                return
            monitor.emit(phase="collecting declared assets")
            # Account for in-flight browser bodies before fetching a declared
            # resource the browser may already have recorded successfully.
            if pending:
                await asyncio.gather(*tuple(pending))
            existing = {
                canonical_url(e["url"])
                for e in ordered.values()
                if e["method"] == "GET" and e["status"] == 200
            }
            candidates = []
            for frame in page.frames:
                try:
                    discovery = await declared_assets(frame)
                    candidates.extend(discovery["assets"])
                    for sheet in discovery["unreadableStylesheets"]:
                        evidence = {"frame": frame.url, **sheet}
                        if evidence not in a["assetCollection"]["unreadableStylesheets"]:
                            a["assetCollection"]["unreadableStylesheets"].append(evidence)
                except Exception as exc:
                    a["warnings"].append(
                        "Asset discovery failed for frame " + frame.url + ": " + str(exc)[:200]
                    )
            semaphore = asyncio.Semaphore(4)

            async def collect(item):
                nonlocal next_response, total
                async with semaphore:
                    response = None
                    key = ("asset", item["url"])
                    monitor.start_request(key, item["url"], "GET", item["kind"], "declared asset")
                    deadline = asyncio.get_running_loop().time() + request_timeout / 1000
                    try:
                        if total >= 150 * 1024 * 1024:
                            raise RuntimeError("Total resource budget exhausted")
                        target = item["url"]
                        for hop in range(6):
                            pattern = excluded(target, exclude_resources)
                            if pattern:
                                item.update(status="excluded", pattern=pattern, excludedUrl=target)
                                return
                            if urlsplit(target).scheme not in ("http", "https"):
                                raise RuntimeError("Asset redirect is not HTTP(S)")
                            remaining = deadline - asyncio.get_running_loop().time()
                            if remaining <= 0:
                                raise TimeoutError(
                                    "Declared asset redirect chain exceeded request timeout"
                                )
                            response = await context.request.get(
                                target, timeout=max(1, remaining * 1000), max_redirects=0
                            )
                            if response.status not in (301, 302, 303, 307, 308):
                                break
                            location = response.headers.get("location")
                            if not location or hop == 5:
                                raise RuntimeError("Invalid or excessive asset redirects")
                            target = urljoin(target, location)
                            item.setdefault("redirects", []).append(target)
                            await response.dispose()
                            response = None
                        headers = dict(response.headers)
                        mime = headers.get("content-type", "application/octet-stream")
                        if response.status != 200:
                            raise RuntimeError(
                                "Full asset GET returned HTTP " + str(response.status)
                            )
                        if not static_mime(item["kind"], mime):
                            raise RuntimeError("Non-static resource MIME: " + mime)
                        body = await response.body()
                        if len(body) > 20 * 1024 * 1024 or total + len(body) > 150 * 1024 * 1024:
                            raise RuntimeError("Resource byte budget exceeded")
                        headers = {
                            k: v
                            for k, v in headers.items()
                            if k.lower()
                            not in ("set-cookie", "authorization", "proxy-authorization")
                        }
                        rec = entry(item["url"], body, mime, 200, headers=headers)
                        rec.update(
                            resourceType=item["kind"],
                            collectionMethod="HTTP collection of declared resource",
                        )
                        ordered[next_response] = rec
                        next_response += 1
                        total += len(body)
                        monitor.state.update(responses=len(ordered), bytes=total)
                        item.update(status="recorded", bytes=len(body), finalUrl=response.url)
                        if "HTTP collection of declared resources" not in a["collectionMethod"]:
                            a["collectionMethod"] += " + HTTP collection of declared resources"
                    except Exception as exc:
                        item.update(status="failed", error=str(exc)[:500])
                        a["warnings"].append(
                            "Declared asset collection failed: "
                            + item["url"]
                            + ": "
                            + str(exc)[:200]
                        )
                    finally:
                        if response is not None:
                            await response.dispose()
                        monitor.end_request(key)

            jobs = []
            for candidate in candidates:
                if urlsplit(candidate["url"]).scheme not in ("http", "https"):
                    continue
                key = canonical_url(candidate["url"])
                if key in existing or key in collected:
                    continue
                collected.add(key)
                if excluded(candidate["url"], exclude_resources):
                    a["assetCollection"]["resources"].append(
                        {
                            **candidate,
                            "status": "excluded",
                            "pattern": excluded(candidate["url"], exclude_resources),
                        }
                    )
                    continue
                if (
                    len(jobs)
                    + sum(x["status"] != "excluded" for x in a["assetCollection"]["resources"])
                    >= asset_limit
                ):
                    a["assetCollection"]["limitReached"] = True
                    a["assetCollection"].setdefault("skipped", []).append(
                        {**candidate, "reason": "asset count limit"}
                    )
                    continue
                candidate["status"] = "pending"
                jobs.append(candidate)
            a["assetCollection"]["resources"].extend(jobs)
            await asyncio.gather(*(collect(item) for item in jobs))
            if a["assetCollection"].get("limitReached"):
                warning = "Declared asset collection limit reached: " + str(asset_limit)
                if warning not in a["warnings"]:
                    a["warnings"].append(warning)

        try:
            monitor.emit(phase="loading page")
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if response is None or response.status >= 400:
                raise RuntimeError("Entry document could not be loaded")
            a["url"] = page.url
            plan = CrawlPlan(page.url, depth, max_pages, include_paths, exclude_paths)
            a["crawl"] = plan.report
            monitor.emit(phase="waiting for page readiness", url=page.url)
            if recipe.get("ready"):
                # An unrelated frame can keep document.readyState interactive
                # indefinitely. The explicit recipe defines captured readiness.
                a["readiness"] = "explicit unobstructed ready selector and state assertions"
            else:
                a["readiness"] = (
                    "DOM loaded, render wait and bounded load/request settling; not a source-usability assertion"
                )
            await check_state(await recipe_scope(page, recipe), recipe, timeout=timeout)
            if recipe.get("settle_ms"):
                await page.wait_for_timeout(recipe["settle_ms"])

            async def checkpoint(name, spec=None, scroll=False):
                monitor.emit(phase="checkpoint", checkpoint=name, url=page.url)
                checkpoint_started = asyncio.get_running_loop().time()
                warnings_before = len(a["warnings"])
                # A stalled async script/image can hold readyState at interactive
                # after usable content is rendered. Share the checkpoint deadline
                # instead of making depth zero fail before retaining an archive.
                await settle(
                    wait_for_load=name == "initial" and not depth and not recipe.get("ready")
                )
                # Scrolling can close popovers and change explicit recipe state.
                # Warm only newly loaded pages, before interaction recording.
                if scroll_steps and scroll:
                    a["scrollActions"].append(
                        {"checkpoint": name, **await warm_scroll(page, scroll_steps)}
                    )
                    await settle()
                await audit_declared_resources()
                await collect_assets()
                monitor.emit(phase="saving checkpoint")
                # Check again at the actual saved checkpoint: an overlay can
                # appear during settling or lazy-resource collection.
                spec = spec or {}
                readiness = await check_state(await recipe_scope(page, spec), spec, timeout=timeout)
                a["sourceChecks"].append({"checkpoint": name, "url": page.url, **readiness})
                current = page_url(page.url)
                current_route = route(page.url)
                if current_route not in a["routes"]:
                    a["routes"].append(current_route)
                if depth:
                    links = await page.locator("a[href],area[href]").evaluate_all(
                        'els => els.map(e => ({href:e.href,download:e.hasAttribute("download")}))'
                    )
                    rendered_links.setdefault(current, []).extend(links)
                if not any(p["url"] == current for p in a["pages"]):
                    document_url = await page.evaluate(
                        'performance.getEntriesByType("navigation")[0]?.name || location.href'
                    )
                    cookies = []
                    if capture_cookies:
                        initial_cookie = await page.evaluate('window.__offlineInitialCookie || ""')
                        cookie_meta = await context.cookies([document_url])
                        for part in initial_cookie.split(";"):
                            if "=" not in part:
                                continue
                            cookie_name, value = part.strip().split("=", 1)
                            meta = next(
                                (
                                    c
                                    for c in cookie_meta
                                    if c["name"] == cookie_name and not c["httpOnly"]
                                ),
                                None,
                            )
                            if meta:
                                cookies.append(
                                    {
                                        "name": cookie_name,
                                        "value": value,
                                        "domain": meta["domain"].lstrip("."),
                                        "hostOnly": not meta["domain"].startswith("."),
                                        "path": meta["path"],
                                        "secure": meta["secure"],
                                        "expires": int(meta["expires"] * 1000)
                                        if meta["expires"] > 0
                                        else 9007199254740991,
                                    }
                                )
                    a["pages"].append(
                        {
                            "url": current,
                            "documentUrl": document_url,
                            "title": await page.title(),
                            "base": await page.evaluate("document.baseURI"),
                            "cookieSeed": cookies,
                        }
                    )
                assets = await page.locator("img").evaluate_all(
                    'els => els.filter(e=>e.currentSrc).map(e=>({src:e.getAttribute("src"),currentSrc:e.currentSrc}))'
                )
                a.setdefault("renderedAssets", {})[current] = assets
                a["frameURLs"] = list(
                    dict.fromkeys(
                        a.get("frameURLs", [])
                        + [
                            f.url
                            for f in page.frames
                            if f != page.main_frame and f.url.startswith(("http:", "https:"))
                        ]
                    )
                )
                a["snapshots"].append({"name": name, "url": page.url, "html": await page.content()})
                await asyncio.wait_for(
                    page.screenshot(path=str(capture_dir / (name + ".png")), full_page=False),
                    timeout=timeout / 1000,
                )
                monitor.diagnostics["pages"].append(
                    {
                        "checkpoint": name,
                        "url": page.url,
                        "seconds": round(asyncio.get_running_loop().time() - checkpoint_started, 3),
                        "newWarnings": len(a["warnings"]) - warnings_before,
                    }
                )
                monitor.emit(
                    "checkpoint_saved", phase="checkpoint saved", pagesSaved=len(a["pages"])
                )

            await checkpoint("initial", recipe, scroll=True)
            for i, step in enumerate(recipe.get("steps", [])):
                monitor.emit(
                    phase="recipe action " + str(i + 1),
                    checkpoint=step.get("checkpoint"),
                    url=page.url,
                )
                await perform_action(page, step)
                scope = await recipe_scope(page, step)
                if step.get("wait_for"):
                    await scope.locator(step["wait_for"]).first.wait_for(state="visible")
                await check_state(scope, step, timeout=timeout)
                if step.get("settle_ms"):
                    await page.wait_for_timeout(step["settle_ms"])
                if step.get("checkpoint"):
                    await checkpoint(step["checkpoint"], step)
            if discover_interactions:
                discovery = a["interactionDiscovery"]
                seen_decisions = set()
                tried = set()
                current_action = None

                def add_decision(item):
                    key = (item.get("selector"), item.get("status"), item.get("reason"))
                    if key in seen_decisions:
                        return
                    seen_decisions.add(key)
                    if len(discovery["decisions"]) < 500:
                        discovery["decisions"].append(item)
                    else:
                        discovery["decisionReportTruncated"] = True

                async def discovery_guard(request_route):
                    request = request_route.request
                    reason = None
                    if request.method.upper() not in ("GET", "HEAD", "OPTIONS"):
                        reason = "non-read HTTP method"
                    elif request.is_navigation_request() and request.frame == page.main_frame:
                        reason = plan.exclusion(request.url)
                    if reason:
                        discovery_blocked_requests.add(request)
                        evidence = {"url": request.url, "method": request.method, "reason": reason}
                        discovery.setdefault("blockedRequests", []).append(evidence)
                        if current_action is not None:
                            current_action.setdefault("blockedRequests", []).append(evidence)
                        await request_route.abort("blockedbyclient")
                    else:
                        await request_route.fallback()

                await page.route("**/*", discovery_guard)
                try:
                    while discovery["attempted"] < max_actions:
                        candidates, skipped = await scan_candidates(
                            page, include_actions, exclude_actions
                        )
                        for item in skipped:
                            add_decision(item)
                        candidate = next(
                            (item for item in candidates if item["selector"] not in tried), None
                        )
                        if candidate is None:
                            discovery["stopReason"] = "no untried eligible controls"
                            break
                        tried.add(candidate["selector"])
                        discovery["attempted"] += 1
                        decision = {**candidate, "status": "attempting", "beforeUrl": page.url}
                        current_action = decision
                        monitor.emit(
                            phase="automatic interaction " + str(discovery["attempted"]),
                            url=page.url,
                        )
                        before = await state_fingerprint(page)
                        before_text = await page.locator("body").inner_text()
                        try:
                            target = page.locator(candidate["selector"]).first
                            await target.click(trial=True)
                            await target.click()
                            await settle()
                            if decision.get("blockedRequests"):
                                decision.update(
                                    status="blocked",
                                    reason="interaction attempted a blocked request",
                                )
                                add_decision(decision)
                                discovery["stopReason"] = (
                                    "blocked request may have changed client state"
                                )
                                break
                            after = await state_fingerprint(page)
                            if after == before:
                                decision.update(
                                    status="skipped",
                                    reason="no material URL or DOM change",
                                    afterUrl=page.url,
                                )
                                add_decision(decision)
                                continue
                            number = len(discovery["steps"]) + 1
                            checkpoint_name = "discovered-" + str(number)
                            existing = {item["name"] for item in a["snapshots"]}
                            while checkpoint_name in existing:
                                number += 1
                                checkpoint_name = "discovered-" + str(number)
                            step = {
                                "action": "click",
                                "selector": candidate["selector"],
                                "checkpoint": checkpoint_name,
                            }
                            if route(page.url) != route(decision["beforeUrl"]):
                                step["route"] = route(page.url)
                            expected = new_visible_text(
                                before_text, await page.locator("body").inner_text()
                            )
                            if expected:
                                step["wait_for"] = "text=" + json.dumps(
                                    expected, ensure_ascii=False
                                )
                                step["expect_text"] = expected
                            await checkpoint(checkpoint_name, step)
                            discovery["steps"].append(step)
                            discovery["recorded"] += 1
                            decision.update(
                                status="recorded", checkpoint=checkpoint_name, afterUrl=page.url
                            )
                            add_decision(decision)
                        except Exception as exc:
                            if decision.get("blockedRequests"):
                                decision.update(
                                    status="blocked",
                                    reason="interaction attempted a blocked request",
                                )
                                discovery["stopReason"] = (
                                    "blocked request may have changed client state"
                                )
                            else:
                                decision.update(status="failed", reason=str(exc)[:500])
                            add_decision(decision)
                            if decision["status"] == "blocked":
                                break
                        finally:
                            current_action = None
                    else:
                        discovery["limitReached"] = True
                        discovery["stopReason"] = "action limit reached"
                    discovery["status"] = (
                        "limited"
                        if discovery.get("limitReached")
                        else (
                            "blocked"
                            if any(
                                item.get("status") == "blocked" for item in discovery["decisions"]
                            )
                            else "partial"
                            if any(
                                item.get("status") == "failed" for item in discovery["decisions"]
                            )
                            else "completed"
                        )
                    )
                finally:
                    await page.unroute("**/*", discovery_guard)
            if depth:
                # Only read navigation links. Never submit forms or click arbitrary
                # buttons, and block cross-origin top-level redirects while crawling.
                async def restrict_navigation(request_route):
                    request = request_route.request
                    if static_failure_circuit.suppress(
                        request.method, request.url, request.resource_type
                    ):
                        circuit_suppressed_requests.add(request)
                        await request_route.abort("blockedbyclient")
                        return
                    cached = static_response_cache.lookup(
                        request.method, request.url, request.resource_type, request.headers
                    )
                    if cached is not None:
                        cache_fulfilled_requests.add(request)
                        try:
                            await request_route.fulfill(
                                status=cached["status"],
                                headers=cached["headers"],
                                body=cached["body"],
                            )
                        except Exception:
                            cache_fulfilled_requests.discard(request)
                            raise
                        static_response_cache.fulfilled(cached)
                        return
                    if request.is_navigation_request() and request.frame == page.main_frame:
                        reason = plan.exclusion(request.url)
                        if reason:
                            plan.skip(request.url, page.url, "redirect: " + reason)
                            await request_route.abort()
                            return
                    await request_route.fallback()

                await page.route("**/*", restrict_navigation)
                plan.discover(plan.start, 0, rendered_links.get(plan.start, []))
                while (item := plan.next()) is not None:
                    monitor.emit(
                        phase="loading page",
                        url=item["url"],
                        visit=len(plan.report["pages"]),
                        checkpoint=None,
                    )
                    if total >= 150 * 1024 * 1024:
                        item.update(status="not_recorded", error="Resource budget exhausted")
                        continue
                    try:
                        if item["url"] not in rendered_links:
                            response = await page.goto(
                                item["url"], wait_until="domcontentloaded", timeout=timeout
                            )
                            if response is None or response.status >= 400:
                                raise RuntimeError("Linked document could not be loaded")
                            if "text/html" not in response.headers.get("content-type", ""):
                                raise RuntimeError("Linked resource is not HTML")
                            await checkpoint(
                                "crawl-" + str(len(plan.report["pages"]) - 1), scroll=True
                            )
                        else:
                            item["reusedRecording"] = True
                        item["finalUrl"] = (
                            page_url(page.url) if not item.get("reusedRecording") else item["url"]
                        )
                        item["status"] = "recorded"
                        if item["finalUrl"] != item["url"]:
                            a.setdefault("pageAliases", {})[item["url"]] = item["finalUrl"]
                            # Also permit the originally requested route in replay.
                            if route(item["url"]) not in a["routes"]:
                                a["routes"].append(route(item["url"]))
                        plan.discover(
                            item["finalUrl"],
                            item["depth"],
                            rendered_links.get(item["finalUrl"], []),
                        )
                    except Exception as exc:
                        item.update(status="failed", error=str(exc))
                        a["warnings"].append("Linked page capture failed: " + item["url"])
                        monitor.emit("page_failed", phase="page failed")
                        monitor.emit(phase="collecting failed-page evidence")
                        try:
                            await asyncio.wait_for(
                                page.screenshot(
                                    path=str(
                                        capture_dir
                                        / ("failed-" + str(len(plan.report["pages"]) - 1) + ".png")
                                    ),
                                    timeout=5000,
                                ),
                                timeout=5,
                            )
                        except Exception as screenshot_error:
                            item["screenshotError"] = (
                                str(screenshot_error) or "Failure screenshot exceeded 5000 ms"
                            )
                # Keep the scope guard installed until context.close(). Removing
                # interception here can wait for a timed-out navigation's server
                # response and undo the navigation deadline.
            # Fix the recording boundary before draining. Polling pages otherwise
            # keep admitting new responses and can make this loop run forever.
            context.remove_listener("response", on_response)
            recording_open = False
            monitor.diagnostics["recordingCutoff"] = monitor.snapshot()
            monitor.emit(phase="finishing admitted response bodies")
            if pending:
                await asyncio.gather(*tuple(pending))
            a["entries"] = [ordered[i] for i in sorted(ordered)]
            # If observations overflowed the bound, no response can be proven
            # exclusively asynchronous, so retain every XHR body eagerly.
            monitor.diagnostics["xhrModes"] = annotate_xhr_modes(
                a["entries"], {} if xhr_modes_truncated else xhr_modes
            )
            monitor.diagnostics["xhrModes"]["observations"] = xhr_mode_events
            monitor.diagnostics["xhrModes"]["truncated"] = xhr_modes_truncated
            monitor.emit(phase="checking script integrity")
            total = await complete_scripts(
                a,
                context.request,
                timeout=request_timeout,
                exclusions=exclude_resources,
                total=total,
                monitor=monitor,
            )
            available = {
                canonical_url(e["url"])
                for e in a["entries"]
                if e["method"] == "GET" and 200 <= e["status"] < 300 and e["status"] != 206
            }
            for (key, kind), item in required.items():
                pattern = excluded(item["url"], exclude_resources)
                status = "excluded" if pattern else "recorded" if key in available else "missing"
                evidence = {**item, "status": status}
                if pattern:
                    evidence["pattern"] = pattern
                a["declaredResourceAudit"]["resources"].append(evidence)
                if status == "missing":
                    warning = f"Declared {kind} missing from capture: " + item["url"]
                    if warning not in a["warnings"]:
                        a["warnings"].append(warning)
            a["environment"] = {
                "engine": engine,
                "browser": browser.version,
                "viewport": {"width": 1363, "height": 936},
                "locale": "en-US",
                "timezone": "UTC",
                "serviceWorkers": "blocked",
                "chromiumSandbox": launch_options.get("chromium_sandbox"),
            }
            a["privacyAudit"] = privacy_audit(a)
            monitor.emit(phase="writing archive", bytes=total)
            await asyncio.to_thread(save, a, output)
            monitor.emit("completed", phase="capture finished", warningCount=len(a["warnings"]))
            crawl_failures = [
                p for p in a["crawl"]["pages"] if p.get("status") in ("failed", "not_recorded")
            ]
            capture_complete = not a["warnings"] and not crawl_failures
            return {
                "archive": str(output),
                "responses": len(a["entries"]),
                "bytes": total,
                "warnings": a["warnings"],
                "crawl": a["crawl"],
                "status": "recorded" if capture_complete else "failed",
                "captureComplete": capture_complete,
                "collectionMethod": a["collectionMethod"],
                "assetCollection": a["assetCollection"],
                "declaredResourceAudit": a["declaredResourceAudit"],
                "privacyAudit": a["privacyAudit"],
                "scrollActions": a["scrollActions"],
                "screenshots": str(capture_dir),
                "interactionDiscovery": a["interactionDiscovery"],
                "sourceChecks": a["sourceChecks"],
                "scriptIntegrity": a["scriptIntegrity"],
                "captureDiagnostics": monitor.diagnostics,
            }
        except asyncio.CancelledError:
            monitor.emit("cancelled", phase="capture cancelled")
            report = reports / (Path(output).stem + "-failure.json")
            report.write_text(
                json.dumps(
                    {
                        "status": "cancelled",
                        "captureDiagnostics": monitor.diagnostics,
                        "progress": monitor.snapshot(),
                    },
                    indent=2,
                )
            )
            raise
        except Exception as exc:
            monitor.emit("failed", phase="capture failed")
            diagnostics = {
                "url": page.url,
                "error": str(exc),
                "warnings": a["warnings"],
                "captureDiagnostics": monitor.diagnostics,
                "progress": monitor.snapshot(),
            }
            if isinstance(exc, ReadinessError):
                diagnostics["readiness"] = exc.evidence
            try:
                diagnostics["readyState"] = await asyncio.wait_for(
                    page.evaluate("document.readyState"), timeout=2
                )
                failure = reports / (Path(output).stem + "-failure.png")
                failure.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.wait_for(page.screenshot(path=str(failure), timeout=5000), timeout=5)
                diagnostics["screenshot"] = str(failure)
            except Exception as diagnostic_exc:
                diagnostics["diagnosticError"] = (
                    str(diagnostic_exc) or "Failure evidence exceeded its timeout"
                )
            report = reports / (Path(output).stem + "-failure.json")
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps(diagnostics, indent=2))
            raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            context.remove_listener("response", on_response)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*tuple(pending), return_exceptions=True)
            await context.close()
            await browser.close()


async def validate(
    path,
    engine="chromium",
    recipe_path=None,
    report_path=None,
    *,
    visual_baseline=None,
    max_visual_change=0.01,
    ready_selector=None,
    timeout=15000,
):
    """Run on a machine with Playwright browsers installed. Never fulfil requests."""
    from playwright.async_api import async_playwright

    result = {
        "file": str(Path(path).resolve()),
        "engine": engine,
        "networkAttempts": [],
        "errors": [],
        "consoleErrors": [],
        "checks": [],
        "checkpoints": [],
        "diagnosticErrors": [],
        "visualChecks": [],
    }
    if not 0 <= max_visual_change <= 1:
        raise ValueError("max visual change must be between 0 and 1")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("validation timeout must be positive")
    recipe = read_recipe(recipe_path, ready_selector)
    prefix = (
        Path(report_path).with_suffix("")
        if report_path
        else Path("reports") / (Path(path).stem + "-" + engine)
    )
    prefix.parent.mkdir(parents=True, exist_ok=True)

    def brief(text):
        return re.sub(r"data:[^\s\"\']{200,}", "[embedded data URL]", str(text))[:4000]

    async with async_playwright() as pw:
        launch_options = browser_launch_options(engine)
        browser = await getattr(pw, engine).launch(**launch_options)
        result["environment"] = {
            "browser": browser.version,
            "playwright": version("playwright"),
            "python": platform.python_version(),
            "viewport": {"width": 1363, "height": 936},
            "offline": True,
            "serviceWorkers": "blocked",
            "chromiumSandbox": launch_options.get("chromium_sandbox"),
            "locale": "en-US",
            "timezone": "UTC",
            "transport": "file://",
            "harnessFulfilsResources": False,
            "validationTimeoutMs": timeout,
        }
        context = await browser.new_context(
            offline=True,
            viewport={"width": 1363, "height": 936},
            locale="en-US",
            timezone_id="UTC",
            service_workers="block",
        )

        async def block(route):
            if urlsplit(route.request.url).scheme in ("http", "https", "ws", "wss"):
                result["networkAttempts"].append(route.request.url)
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", block)
        page = await context.new_page()
        page.set_default_timeout(timeout)
        page.on("pageerror", lambda e: result["errors"].append(brief(e)))
        page.on(
            "console",
            lambda m: result["consoleErrors"].append(brief(m.text)) if m.type == "error" else None,
        )

        async def checkpoint(name, spec):
            evidence = {"name": name, "url": page.url}
            active = None
            try:
                active = await replay_page(page)
                evidence["title"] = await page.title()
                evidence["text"] = await active.locator("body").inner_text(timeout=2000)
                evidence["images"] = await active.locator(spec.get("images", "img")).evaluate_all(
                    "els => els.map(e => ({complete:e.complete,width:e.naturalWidth,height:e.naturalHeight}))"
                )
                evidence["capturedUrl"] = await active.evaluate(
                    "window.__offlineLocation?.href || window.__OFFLINE_ARCHIVE__?.url || location.href"
                )
                report = active.locator("#offline-snapshot-status pre")
                if await report.count():
                    result["runtimeReport"] = json.loads(await report.text_content(timeout=2000))
                    evidence["runtimeReport"] = result["runtimeReport"]
                evidence["frameRuntimeReports"] = []
                for frame in page.frames:
                    if frame == active:
                        continue
                    frame_report = frame.locator("#offline-snapshot-status pre")
                    if await frame_report.count():
                        evidence["frameRuntimeReports"].append(
                            json.loads(await frame_report.text_content(timeout=2000))
                        )
            except Exception as exc:
                result["diagnosticErrors"].append(brief(exc))
            # Even an initial assertion failure must leave a screenshot.
            try:
                screenshot = str(prefix) + "-" + re.sub(r"[^a-zA-Z0-9_-]", "-", name) + ".png"
                await page.screenshot(path=screenshot, timeout=5000)
                evidence["screenshot"] = screenshot
                if visual_baseline:
                    baseline = Path(visual_baseline)
                    if baseline.is_dir():
                        baseline = baseline / (
                            "initial.png" if name == "file-open" else name + ".png"
                        )
                    if name == "file-open" or Path(visual_baseline).is_dir():
                        from .visual import compare

                        visual_image = str(prefix) + "-" + name + "-appearance.png"
                        panel = active.locator("#offline-snapshot-status") if active else None
                        visibility = []
                        if panel and await panel.count():
                            visibility = await panel.evaluate_all(
                                "els=>els.map(e=>[e.style.getPropertyValue('visibility'),e.style.getPropertyPriority('visibility')])"
                            )
                            await panel.evaluate_all(
                                "els=>els.forEach(e=>e.style.setProperty('visibility','hidden','important'))"
                            )
                        try:
                            # Explicitly mutate the inner replay document because
                            # screenshot styles do not reliably cross blob-frame
                            # origin boundaries in Chromium.
                            await page.screenshot(path=visual_image)
                        finally:
                            if panel and visibility:
                                await panel.evaluate_all(
                                    "(els,states)=>els.forEach((e,i)=>states[i][0]?e.style.setProperty('visibility',states[i][0],states[i][1]):e.style.removeProperty('visibility'))",
                                    visibility,
                                )
                        comparison = compare(
                            baseline,
                            visual_image,
                            str(prefix) + "-" + name + "-difference.png",
                            max_changed_ratio=max_visual_change,
                        )
                        comparison.update(
                            checkpoint=name,
                            excluded="Only offline diagnostic panel, hidden without changing layout",
                        )
                        result["visualChecks"].append(comparison)
            except Exception as exc:
                result["diagnosticErrors"].append(brief(exc))
            result["checkpoints"].append(evidence)

        try:
            sequence = [("file-open", recipe)] + [
                (s.get("checkpoint", f"step-{i + 1}"), s)
                for i, s in enumerate(recipe.get("steps", []))
            ]
            failed = False
            for name, step in sequence:
                if failed:
                    result["checks"].append(
                        {
                            "name": name,
                            "passed": False,
                            "status": "not_run",
                            "reason": "Previous checkpoint failed",
                        }
                    )
                    continue
                try:
                    if name == "file-open":
                        await page.goto(Path(path).resolve().as_uri(), wait_until="load")
                        active = await replay_page(page)
                    else:
                        active = await replay_page(page)
                        await perform_action(active, step)
                    active = await replay_page(page)
                    scope = await recipe_scope(active, step)
                    if step.get("wait_for"):
                        await scope.locator(step["wait_for"]).first.wait_for(state="visible")
                    await check_state(scope, step, timeout=timeout)
                    await page.wait_for_timeout(step.get("settle_ms", 500))
                    readiness = await check_state(scope, step, timeout=timeout)
                    result["checks"].append(
                        {"name": name, "passed": True, "status": "passed", "readiness": readiness}
                    )
                except Exception as exc:
                    result["checks"].append(
                        {"name": name, "passed": False, "status": "failed", "error": brief(exc)}
                    )
                    if isinstance(exc, ReadinessError):
                        result["checks"][-1]["readiness"] = exc.evidence
                    failed = True
                finally:
                    await checkpoint(name, step)
            result["title"] = await page.title()
        except Exception as exc:
            result["errors"].append(brief(exc))
        finally:
            await context.close()
            await browser.close()
    result["interactionsPassed"] = bool(result["checks"]) and all(
        c["passed"] for c in result["checks"]
    )
    reports = [
        r
        for c in result["checkpoints"]
        for r in [c.get("runtimeReport", {}), *c.get("frameRuntimeReports", [])]
    ]
    result["unsupportedAPIs"] = [
        item
        for r in reports
        for item in r.get("blocked", [])
        if item.get("kind") in ("Worker", "SharedWorker", "WebSocket", "EventSource", "iframe")
    ]
    result["passed"] = (
        result["interactionsPassed"]
        and "runtimeReport" in result
        and not result["networkAttempts"]
        and not result["errors"]
        and not result["consoleErrors"]
        and not result["diagnosticErrors"]
        and not result["unsupportedAPIs"]
        and not any(r.get(k) for r in reports for k in ("misses", "errors", "violations"))
        and all(v["passed"] for v in result["visualChecks"])
    )
    return result

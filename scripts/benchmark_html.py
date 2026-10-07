"""Compare two standalone snapshots through file:// in both supported browsers."""

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from playwright.async_api import async_playwright


def median(rows, field):
    return round(statistics.median(row[field] for row in rows), 1)


async def run(args):
    files = {"before": args.before.resolve(), "after": args.after.resolve()}
    rows = []
    environment = {}
    async with async_playwright() as playwright:
        for engine in ("chromium", "firefox"):
            browser = await getattr(playwright, engine).launch(
                **({"chromium_sandbox": True} if engine == "chromium" else {})
            )
            environment[engine] = browser.version
            order = []
            for index in range(args.runs):
                order.extend(("before", "after") if index % 2 == 0 else ("after", "before"))
            for sequence, label in enumerate(order, 1):
                context = await browser.new_context(offline=True, service_workers="block")
                network_attempts = []

                async def block(route, attempts=network_attempts):
                    if route.request.url.startswith(("http:", "https:")):
                        attempts.append(route.request.url)
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route("**/*", block)
                page = await context.new_page()
                console_errors = []
                page_errors = []
                page.on(
                    "console",
                    lambda message, errors=console_errors: (
                        errors.append(message.text) if message.type == "error" else None
                    ),
                )
                page.on("pageerror", lambda error, errors=page_errors: errors.append(str(error)))
                started = time.perf_counter()
                error = None
                try:
                    await page.goto(
                        files[label].as_uri(),
                        wait_until="domcontentloaded",
                        timeout=args.timeout_ms,
                    )
                    await (
                        page.frame_locator("#offline-page")
                        .locator("#offline-snapshot-status")
                        .wait_for(timeout=args.timeout_ms)
                    )
                except Exception as exception:
                    error = str(exception)
                ready_ms = (time.perf_counter() - started) * 1000
                navigation = await page.evaluate(
                    "performance.getEntriesByType('navigation')[0]?.toJSON()"
                )
                rows.append(
                    {
                        "engine": engine,
                        "browserVersion": browser.version,
                        "sequence": sequence,
                        "artifact": label,
                        "readyMs": round(ready_ms, 1),
                        "outerDOMContentLoadedMs": round(
                            navigation.get("domContentLoadedEventEnd", 0), 1
                        ),
                        "error": error,
                        "consoleErrors": console_errors,
                        "pageErrors": page_errors,
                        "networkAttempts": network_attempts,
                    }
                )
                await context.close()
            await browser.close()

    summaries = {}
    for engine in ("chromium", "firefox"):
        before = [row for row in rows if row["engine"] == engine and row["artifact"] == "before"]
        after = [row for row in rows if row["engine"] == engine and row["artifact"] == "after"]
        before_dcl = median(before, "outerDOMContentLoadedMs")
        after_dcl = median(after, "outerDOMContentLoadedMs")
        summaries[engine] = {
            "beforeReadyMedianMs": median(before, "readyMs"),
            "afterReadyMedianMs": median(after, "readyMs"),
            "beforeOuterDOMContentLoadedMedianMs": before_dcl,
            "afterOuterDOMContentLoadedMedianMs": after_dcl,
            "outerDOMContentLoadedChangePercent": round(
                (after_dcl - before_dcl) / before_dcl * 100, 1
            ),
        }
    before_bytes = files["before"].stat().st_size
    after_bytes = files["after"].stat().st_size
    clean = all(
        not row["error"]
        and not row["consoleErrors"]
        and not row["pageErrors"]
        and not row["networkAttempts"]
        for row in rows
    )
    faster = all(
        value["afterOuterDOMContentLoadedMedianMs"]
        <= value["beforeOuterDOMContentLoadedMedianMs"] * 0.85
        for value in summaries.values()
    )
    result = {
        "passed": clean and faster and after_bytes < before_bytes,
        "scope": "Outer container parse plus captured-page readiness through file://; "
        "captured application execution makes total readiness informational.",
        "runsPerArtifact": args.runs,
        "files": {
            "before": {"path": str(files["before"]), "bytes": before_bytes},
            "after": {"path": str(files["after"]), "bytes": after_bytes},
            "sizeChangePercent": round((after_bytes - before_bytes) / before_bytes * 100, 1),
        },
        "environment": environment,
        "summaries": summaries,
        "runs": rows,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--runs", type=int, default=4)
    parser.add_argument("--timeout-ms", type=int, default=120000)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    if args.runs < 2:
        parser.error("--runs must be at least 2")
    if not args.before.is_file() or not args.after.is_file():
        parser.error("both HTML files must exist")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()

"""Compare standalone snapshot renderer memory through file:// on Linux."""

import argparse
import asyncio
import json
import os
import statistics
import time
from pathlib import Path

from playwright.async_api import async_playwright


def median(rows, field):
    return round(statistics.median(row[field] for row in rows), 1)


def descendant_rss_kib(root_pid):
    """Sum resident memory for descendants of this benchmark process."""
    processes = {}
    proc = Path("/proc")
    for item in proc.iterdir():
        if not item.name.isdigit():
            continue
        try:
            stat = (item / "stat").read_text()
            rest = stat[stat.rfind(")") + 2 :].split()
            parent = int(rest[1])
            status = (item / "status").read_text()
            rss = next(
                int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS:")
            )
        except (FileNotFoundError, PermissionError, StopIteration, ValueError):
            continue
        processes[int(item.name)] = (parent, rss)
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _) in processes.items():
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return sum(processes[pid][1] for pid in descendants if pid != root_pid and pid in processes)


async def sample_memory(root_pid, samples, stopped, interval):
    while not stopped.is_set():
        samples.append(descendant_rss_kib(root_pid))
        try:
            await asyncio.wait_for(stopped.wait(), interval)
        except asyncio.TimeoutError:
            pass


async def measure(playwright, engine, path, label, sequence, args):
    launch = {"chromium_sandbox": True} if engine == "chromium" else {}
    browser = await getattr(playwright, engine).launch(**launch)
    browser_version = browser.version
    await asyncio.sleep(0.25)
    baseline = descendant_rss_kib(os.getpid())
    samples = [baseline]
    stopped = asyncio.Event()
    sampler = asyncio.create_task(
        sample_memory(os.getpid(), samples, stopped, args.sample_interval_ms / 1000)
    )
    network_attempts = []
    console_errors = []
    page_errors = []
    error = None
    ready_ms = None
    navigation = None
    try:
        context = await browser.new_context(offline=True, service_workers="block")

        async def block(route):
            if route.request.url.startswith(("http:", "https:", "ws:", "wss:")):
                network_attempts.append(route.request.url)
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", block)
        page = await context.new_page()
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text) if message.type == "error" else None
            ),
        )
        page.on("pageerror", lambda value: page_errors.append(str(value)))
        started = time.perf_counter()
        try:
            await page.goto(path.as_uri(), wait_until="domcontentloaded", timeout=args.timeout_ms)
            await (
                page.frame_locator("#offline-page")
                .locator("#offline-snapshot-status")
                .wait_for(timeout=args.timeout_ms)
            )
            ready_ms = round((time.perf_counter() - started) * 1000, 1)
            navigation = await page.evaluate(
                "performance.getEntriesByType('navigation')[0]?.toJSON()"
            )
            await page.wait_for_timeout(args.settle_ms)
        except Exception as exception:
            error = str(exception)
        finally:
            await context.close()
    finally:
        stopped.set()
        await sampler
        await browser.close()
    peak = max(samples)
    return {
        "engine": engine,
        "browserVersion": browser_version,
        "sequence": sequence,
        "artifact": label,
        "readyMs": ready_ms,
        "outerDOMContentLoadedMs": round((navigation or {}).get("domContentLoadedEventEnd", 0), 1),
        "idleBrowserRssKiB": baseline,
        "peakRssKiB": peak,
        "peakDeltaRssKiB": max(0, peak - baseline),
        "samples": len(samples),
        "error": error,
        "consoleErrors": console_errors,
        "pageErrors": page_errors,
        "networkAttempts": network_attempts,
    }


async def run(args):
    files = {"before": args.before.resolve(), "after": args.after.resolve()}
    rows = []
    async with async_playwright() as playwright:
        for engine in ("chromium", "firefox"):
            order = []
            for index in range(args.runs):
                order.extend(("before", "after") if index % 2 == 0 else ("after", "before"))
            for sequence, label in enumerate(order, 1):
                row = await measure(playwright, engine, files[label], label, sequence, args)
                rows.append(row)
                print(
                    f"{engine} {sequence}/{len(order)} {label}: "
                    f"{row['peakDeltaRssKiB']} KiB peak delta",
                    flush=True,
                )

    summaries = {}
    for engine in ("chromium", "firefox"):
        before = [row for row in rows if row["engine"] == engine and row["artifact"] == "before"]
        after = [row for row in rows if row["engine"] == engine and row["artifact"] == "after"]
        before_peak = median(before, "peakDeltaRssKiB")
        after_peak = median(after, "peakDeltaRssKiB")
        summaries[engine] = {
            "beforePeakDeltaMedianKiB": before_peak,
            "afterPeakDeltaMedianKiB": after_peak,
            "peakDeltaChangePercent": round((after_peak - before_peak) / before_peak * 100, 1)
            if before_peak
            else None,
            "beforeReadyMedianMs": median(before, "readyMs"),
            "afterReadyMedianMs": median(after, "readyMs"),
        }
    clean = all(
        not row["error"]
        and not row["consoleErrors"]
        and not row["pageErrors"]
        and not row["networkAttempts"]
        for row in rows
    )
    memory_target = all(
        summary["afterPeakDeltaMedianKiB"]
        <= summary["beforePeakDeltaMedianKiB"] * args.max_peak_ratio
        for summary in summaries.values()
    )
    result = {
        "passed": clean and (memory_target or not args.require_improvement),
        "clean": clean,
        "memoryTargetPassed": memory_target,
        "requiredPeakRatio": args.max_peak_ratio,
        "scope": "Peak Linux RSS of Playwright/browser descendants above an idle "
        "browser baseline while opening standalone file:// HTML.",
        "runsPerArtifact": args.runs,
        "files": {
            name: {"path": str(path), "bytes": path.stat().st_size} for name, path in files.items()
        },
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
    parser.add_argument("--settle-ms", type=int, default=500)
    parser.add_argument("--sample-interval-ms", type=int, default=25)
    parser.add_argument("--max-peak-ratio", type=float, default=0.9)
    parser.add_argument("--require-improvement", action="store_true")
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    if not Path("/proc").is_dir():
        parser.error("renderer RSS benchmarking requires Linux /proc")
    if args.runs < 2:
        parser.error("--runs must be at least 2")
    if min(args.timeout_ms, args.settle_ms, args.sample_interval_ms) <= 0:
        parser.error("timeouts and sampling interval must be positive")
    if not 0 < args.max_peak_ratio <= 1:
        parser.error("--max-peak-ratio must be between zero and one")
    if not args.before.is_file() or not args.after.is_file():
        parser.error("both HTML files must exist")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()

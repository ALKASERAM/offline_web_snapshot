"""Command-line entry point. JSON results on stdout; progress on stderr."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .archive import load, privacy_audit
from .pack import pack
from .readiness import ReadinessError
from .workflow import require_new, save, write_report


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def fraction(value):
    number = float(value)
    if not 0 <= number <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return number


def source_url(value):
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise argparse.ArgumentTypeError("must be an absolute HTTP or HTTPS URL")
    if parts.username is not None or parts.password is not None:
        raise argparse.ArgumentTypeError("must not contain credentials")
    return value


def progress(message):
    print(message, file=sys.stderr, flush=True)


def parser():
    p = argparse.ArgumentParser(
        description="Capture websites into one HTML file and verify offline replay"
    )
    p.add_argument("--version", action="version", version=f"offline-snapshot {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    setup = sub.add_parser(
        "setup", help="Install pinned JavaScript tools and browser binaries (uses network)"
    )
    setup.add_argument(
        "--browsers", nargs="+", choices=["chromium", "firefox"], default=["chromium", "firefox"]
    )
    setup.add_argument(
        "--skip-browsers", action="store_true", help="Install only JavaScript build tools"
    )
    doctor = sub.add_parser(
        "doctor", help="Check parser and browser execution without downloading anything"
    )
    doctor.add_argument(
        "--browsers", nargs="+", choices=["chromium", "firefox"], default=["chromium", "firefox"]
    )
    for command, description in [
        ("save", "Capture, build, and verify in both browsers"),
        ("capture", "Record online pages and retain the archive"),
    ]:
        c = sub.add_parser(command, help=description)
        c.add_argument("url", type=source_url)
        c.add_argument("-o", "--output", required=True)
        c.add_argument("--recipe", help="JSON interaction recipe")
        c.add_argument(
            "--ready-selector",
            help="Entry-page element that must be unobstructed; overrides recipe ready",
        )
        c.add_argument("--headed", action="store_true")
        c.add_argument(
            "--browser",
            choices=["chromium", "firefox"],
            default="chromium",
            help="Capture browser (default: chromium)",
        )
        c.add_argument(
            "--depth",
            "--link-depth",
            dest="depth",
            type=nonnegative,
            default=0,
            help="Link hops: 0=start, 1=its links, 2=one level further (default: 0)",
        )
        c.add_argument(
            "--max-pages",
            type=positive,
            default=25,
            help="Maximum automatic visits, including start and failed visits (default: 25)",
        )
        c.add_argument(
            "--include-path",
            action="append",
            default=[],
            help="Include matching paths; repeatable glob",
        )
        c.add_argument(
            "--exclude-path",
            action="append",
            default=[],
            help="Exclude matching paths; repeatable glob",
        )
        c.add_argument("--page-wait-ms", type=nonnegative, default=1000)
        c.add_argument(
            "--timeout-ms",
            type=positive,
            default=45000,
            help="Capture navigation/action timeout (default: 45000)",
        )
        c.add_argument(
            "--settle-timeout-ms",
            type=positive,
            help="Maximum request-settling wait per checkpoint (default: --timeout-ms)",
        )
        c.add_argument(
            "--request-timeout-ms",
            type=positive,
            help="Response-body/HTTP collection timeout (default: --timeout-ms)",
        )
        c.add_argument(
            "--exclude-resource",
            action="append",
            default=[],
            help="Explicit resource URL omission; repeatable glob",
        )
        c.add_argument(
            "--asset-limit",
            type=nonnegative,
            default=200,
            help="Additional declared static resources to collect (0 disables)",
        )
        c.add_argument("--scroll-steps", type=nonnegative, default=0)
        c.add_argument(
            "--discover-interactions",
            action="store_true",
            help="Safely discover non-form SPA controls (opt-in)",
        )
        c.add_argument(
            "--max-actions",
            type=positive,
            default=25,
            help="Maximum automatic interaction attempts (default: 25)",
        )
        c.add_argument(
            "--include-action",
            action="append",
            default=[],
            help="Limit automatic discovery to matching CSS selectors; repeatable",
        )
        c.add_argument(
            "--exclude-action",
            action="append",
            default=[],
            help="Exclude matching CSS selectors from automatic discovery; repeatable",
        )
        c.add_argument(
            "--capture-cookies",
            action="store_true",
            help="Opt in to retaining script-visible cookie seeds; review before sharing",
        )
        c.add_argument("--report-dir", help="Directory for screenshots and diagnostic reports")
        c.add_argument(
            "--overwrite", action="store_true", help="Replace existing generated outputs explicitly"
        )
        if command == "save":
            c.add_argument(
                "--archive",
                help="Retained recording path (default: output name with .capture.json)",
            )
            c.add_argument(
                "--no-validate",
                action="store_true",
                help="Build without verification; result is marked not_validated",
            )
            c.add_argument(
                "--validation-browsers",
                nargs="+",
                choices=["chromium", "firefox"],
                default=["chromium", "firefox"],
            )
            c.add_argument(
                "--validation-timeout-ms",
                type=positive,
                default=15000,
                help="Offline navigation/action timeout per check (default: 15000)",
            )
            c.add_argument("--max-visual-change", type=fraction, default=0.01)
    b = sub.add_parser("pack", help="Build an existing recording without network access")
    b.add_argument("archive")
    b.add_argument("-o", "--output", required=True)
    b.add_argument("--overwrite", action="store_true")
    v = sub.add_parser(
        "validate", help="Open file:// in fresh offline browsers; never serve or fulfil resources"
    )
    v.add_argument("html")
    v.add_argument("--browser", choices=["chromium", "firefox", "both"], default="chromium")
    v.add_argument("--recipe")
    v.add_argument(
        "--ready-selector",
        help="Entry-page element that must be unobstructed; overrides recipe ready",
    )
    v.add_argument(
        "-o",
        "--output",
        help="JSON report; with both browsers, a summary with separate engine reports",
    )
    v.add_argument(
        "--visual-baseline", help="Same-browser PNG or checkpoint directory; requires one browser"
    )
    v.add_argument(
        "--validation-timeout-ms",
        type=positive,
        default=15000,
        help="Offline navigation/action timeout per check (default: 15000)",
    )
    v.add_argument("--max-visual-change", type=fraction, default=0.01)
    i = sub.add_parser(
        "inspect", help="Report archive scope and potential sensitivity without exposing values"
    )
    i.add_argument("archive")
    i.add_argument("-o", "--output", help="Optional JSON report path")
    i.add_argument(
        "--fail-on-sensitive",
        action="store_true",
        help="Exit 1 when the heuristic finds potentially sensitive state",
    )
    return p


def capture_options(args):
    return dict(
        headed=args.headed,
        timeout=args.timeout_ms,
        depth=args.depth,
        max_pages=args.max_pages,
        include_paths=args.include_path,
        exclude_paths=args.exclude_path,
        page_wait_ms=args.page_wait_ms,
        exclude_resources=args.exclude_resource,
        engine=args.browser,
        asset_limit=args.asset_limit,
        scroll_steps=args.scroll_steps,
        ready_selector=args.ready_selector,
        settle_timeout=args.settle_timeout_ms,
        request_timeout=args.request_timeout_ms,
        capture_cookies=args.capture_cookies,
        discover_interactions=args.discover_interactions,
        max_actions=args.max_actions,
        include_actions=args.include_action,
        exclude_actions=args.exclude_action,
    )


async def validate_command(args):
    from .capture import validate

    engines = ("chromium", "firefox") if args.browser == "both" else (args.browser,)
    if len(engines) > 1 and args.visual_baseline:
        raise ValueError(
            "A visual reference belongs to one browser. Run separate validations with --browser chromium or firefox."
        )
    summary = (
        Path(args.output)
        if args.output
        else Path("reports") / f"{Path(args.html).stem}-{args.browser}.json"
    )
    targets = [summary]
    if len(engines) > 1:
        targets += [summary.with_name(summary.stem + "-" + engine + ".json") for engine in engines]
    if Path(args.html).resolve() in {path.resolve() for path in targets}:
        raise ValueError("Validation reports must not overwrite the input HTML")
    checks = []
    for engine in engines:
        report = (
            summary.with_name(summary.stem + "-" + engine + ".json")
            if len(engines) > 1
            else summary
        )
        progress(f"Verifying file:// offline in {engine}…")
        try:
            check = await validate(
                args.html,
                engine,
                args.recipe,
                report,
                visual_baseline=args.visual_baseline,
                max_visual_change=args.max_visual_change,
                ready_selector=args.ready_selector,
                timeout=args.validation_timeout_ms,
            )
            check["status"] = "passed" if check["passed"] else "failed"
        except Exception as exc:
            check = {
                "file": str(Path(args.html).resolve()),
                "engine": engine,
                "passed": False,
                "status": "error",
                "setupError": str(exc),
            }
        write_report(report, check)
        checks.append({**check, "report": str(report)})
    if len(checks) == 1:
        return checks[0]
    result = {
        "file": str(Path(args.html).resolve()),
        "passed": all(c["passed"] for c in checks),
        "validation": checks,
    }
    result["status"] = (
        "error"
        if any(c["status"] == "error" for c in checks)
        else ("passed" if result["passed"] else "failed")
    )
    write_report(summary, result)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "setup":
            from .toolchain import setup

            result = setup(() if args.skip_browsers else args.browsers, progress=progress)
        elif args.command == "doctor":
            from .toolchain import doctor

            result = asyncio.run(doctor(args.browsers))
            result["status"] = "passed" if result["passed"] else "error"
        elif args.command == "save":
            result = asyncio.run(
                save(
                    args.url,
                    args.output,
                    recipe_path=args.recipe,
                    report_dir=args.report_dir,
                    archive_path=args.archive,
                    browsers=args.validation_browsers,
                    verify=not args.no_validate,
                    overwrite=args.overwrite,
                    max_visual_change=args.max_visual_change,
                    validation_timeout=args.validation_timeout_ms,
                    progress=progress,
                    **capture_options(args),
                )
            )
        elif args.command == "capture":
            from .capture import capture

            require_new([args.output], args.overwrite)
            progress("Capturing page and selected links…")
            result = asyncio.run(
                capture(
                    args.url,
                    args.output,
                    recipe_path=args.recipe,
                    report_dir=args.report_dir,
                    progress=progress,
                    **capture_options(args),
                )
            )
            if "captureComplete" not in result:
                incomplete = result["warnings"] or any(
                    p.get("status") in ("failed", "not_recorded") for p in result["crawl"]["pages"]
                )
                result["status"] = "failed" if incomplete else "recorded"
                result["captureComplete"] = not bool(incomplete)
        elif args.command == "pack":
            require_new([args.output], args.overwrite)
            if Path(args.archive).resolve() == Path(args.output).resolve():
                raise ValueError("Archive and HTML output paths must be different")
            result = pack(load(args.archive), args.output)
            result["status"] = "not_validated"
        elif args.command == "inspect":
            result = privacy_audit(load(args.archive))
            result["archive"] = str(Path(args.archive).resolve())
            result["status"] = (
                "failed"
                if args.fail_on_sensitive and result["containsPotentiallySensitiveData"]
                else "inspected"
            )
            if args.output:
                if Path(args.archive).resolve() == Path(args.output).resolve():
                    raise ValueError("Inspection report must not overwrite the archive")
                write_report(args.output, result)
        else:
            result = asyncio.run(validate_command(args))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result.get("status") == "error":
            return 2
        if result.get("status") == "failed":
            return 1
        return 0
    except ReadinessError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "stage": "source_readiness",
                    "passed": False,
                    "readiness": exc.evidence,
                },
                ensure_ascii=False,
            )
        )
        progress(str(exc))
        return 1
    except KeyboardInterrupt:
        progress("Cancelled. Existing recordings and diagnostic reports are retained.")
        return 130
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        progress("error: " + str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

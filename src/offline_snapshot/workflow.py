"""The same capture/build/verify workflow for the CLI and Python callers."""

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .archive import load
from .capture import capture, validate
from .pack import pack
from .readiness import ReadinessError, read_recipe


def write_report(path, result):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")


def require_new(paths, overwrite=False):
    if not overwrite:
        for path in paths:
            if Path(path).exists():
                raise FileExistsError(
                    f"Output already exists: {path}. Choose a new path or use --overwrite."
                )


async def save(
    url,
    output,
    *,
    recipe_path=None,
    report_dir=None,
    archive_path=None,
    browsers=("chromium", "firefox"),
    verify=True,
    overwrite=False,
    max_visual_change=0.01,
    validation_timeout=15000,
    progress=None,
    **capture_options,
):
    """Capture to a retained archive, build one HTML, and verify offline.

    Returns a JSON-compatible report. A failed browser check is returned, not
    raised; setup/capture/build failures are reported and then raised. Await this
    function in async applications, or call asyncio.run(save(...)) in a script.
    All browser/crawl options are passed to capture(). No downloads are installed
    implicitly. Without a recipe, verification covers opening the entry page.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Capture URL must be an absolute HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Capture URL must not contain credentials")
    if not 0 <= max_visual_change <= 1:
        raise ValueError("max visual change must be between 0 and 1")
    if not isinstance(validation_timeout, (int, float)) or validation_timeout <= 0:
        raise ValueError("validation timeout must be positive")
    engines = list(dict.fromkeys(browsers))
    if verify and (not engines or any(e not in ("chromium", "firefox") for e in engines)):
        raise ValueError("Choose chromium and/or firefox for validation")
    output = Path(output).resolve()
    archive = Path(archive_path).resolve() if archive_path else output.with_suffix(".capture.json")
    reports = (
        Path(report_dir).resolve() if report_dir else (Path("reports") / output.stem).resolve()
    )
    summary = reports / "result.json"
    if output.suffix.lower() != ".html":
        raise ValueError("The output filename must end in .html")
    if len({output, archive, summary}) != 3:
        raise ValueError("HTML, archive and report paths must be different")
    require_new([output, archive, summary], overwrite)
    result = {
        "version": __version__,
        "url": url,
        "output": str(output),
        "archive": str(archive),
        "report": str(summary),
        "status": "running",
        "passed": False,
        "validation": [],
        "stages": {
            "capture": {"status": "running"},
            "build": {"status": "not_run"},
            "verification": {"status": "not_run" if not verify else "pending"},
        },
        "verificationScope": {
            "source": "Only explicit ready selectors are checked; document load alone does not establish source usability.",
            "recipe": str(Path(recipe_path).resolve()) if recipe_path else None,
            "interactions": "recipe checkpoints only"
            if recipe_path
            else "not tested; no recipe supplied",
            "navigation": "Crawled pages are recorded; only recipe navigation is interaction-tested.",
            "visual": "Captured-browser references only; other engines have no matched online reference.",
        },
    }
    stage = "capture"
    try:
        if progress:
            progress("Capturing page and selected links…")
        result["capture"] = await capture(
            url,
            archive,
            recipe_path=recipe_path,
            report_dir=reports,
            progress=progress,
            **capture_options,
        )
        discovery = result["capture"].get("interactionDiscovery", {})
        discovered_steps = discovery.get("steps", [])
        validation_recipe = recipe_path
        if discovered_steps:
            validation_recipe = read_recipe(recipe_path, capture_options.get("ready_selector"))
            validation_recipe["steps"] = [*validation_recipe.get("steps", []), *discovered_steps]
            result["verificationScope"]["interactions"] = (
                "recipe checkpoints plus " if recipe_path else ""
            ) + f"{len(discovered_steps)} automatically discovered safe checkpoint(s)"
        elif discovery.get("enabled"):
            result["verificationScope"]["interactions"] = (
                "automatic discovery recorded no replayable checkpoints; "
                + ("recipe checkpoints only" if recipe_path else "entry page only")
            )
        crawl_failures = [
            p
            for p in result["capture"].get("crawl", {}).get("pages", [])
            if p.get("status") in ("failed", "not_recorded")
        ]
        capture_complete = result["capture"].get(
            "captureComplete", not result["capture"].get("warnings") and not crawl_failures
        )
        result["captureComplete"] = bool(capture_complete)
        result["stages"]["capture"] = {
            "status": "complete" if capture_complete else "incomplete",
            "complete": bool(capture_complete),
            "warningCount": len(result["capture"].get("warnings", [])),
            "failedPageCount": len(crawl_failures),
        }
        stage = "pack"
        result["stages"]["build"] = {"status": "running"}
        if progress:
            progress("Building standalone HTML…")
        result["build"] = await asyncio.to_thread(pack, load(archive), output)
        result["build"]["status"] = "built"
        result["stages"]["build"] = {"status": "built", "bytes": result["build"]["bytes"]}
        stage = "validate"
        if verify:
            result["stages"]["verification"] = {"status": "running"}
            for engine in engines:
                if progress:
                    progress(f"Verifying file:// offline in {engine}…")
                report = reports / f"{engine}.json"
                baseline = (
                    result["capture"]["screenshots"]
                    if engine == capture_options.get("engine", "chromium")
                    else None
                )
                try:
                    check = await validate(
                        output,
                        engine,
                        validation_recipe,
                        report,
                        visual_baseline=baseline,
                        max_visual_change=max_visual_change,
                        ready_selector=capture_options.get("ready_selector"),
                        timeout=validation_timeout,
                    )
                    check["status"] = "passed" if check["passed"] else "failed"
                except Exception as exc:
                    check = {
                        "engine": engine,
                        "passed": False,
                        "status": "error",
                        "setupError": str(exc),
                    }
                write_report(report, check)
                result["validation"].append(
                    {
                        "engine": engine,
                        "passed": check["passed"],
                        "status": check["status"],
                        "report": str(report),
                        "visualStatus": "tested"
                        if baseline and check.get("visualChecks")
                        else "not_run",
                    }
                )
            result["passed"] = result["captureComplete"] and all(
                c["passed"] for c in result["validation"]
            )
            result["status"] = "passed" if result["passed"] else "failed"
            if any(c["status"] == "error" for c in result["validation"]):
                result["status"] = "error"
            result["stages"]["verification"] = {
                "status": "error"
                if result["status"] == "error"
                else ("passed" if all(c["passed"] for c in result["validation"]) else "failed"),
                "browsers": result["validation"],
            }
        else:
            result["status"] = "not_validated"
            result["stages"]["verification"] = {"status": "not_run"}
    except ReadinessError as exc:
        result.update(
            status="failed", stage="source_readiness", readiness=exc.evidence, error=str(exc)
        )
        result["stages"]["capture"] = {"status": "failed", "error": str(exc)}
        raise
    except asyncio.CancelledError:
        result.update(status="cancelled", stage=stage)
        raise
    except Exception as exc:
        result.update(status="error", stage=stage, error=str(exc))
        stage_name = (
            "verification" if stage == "validate" else ("build" if stage == "pack" else "capture")
        )
        result["stages"][stage_name] = {"status": "error", "error": str(exc)}
        raise
    finally:
        write_report(summary, result)
    return result

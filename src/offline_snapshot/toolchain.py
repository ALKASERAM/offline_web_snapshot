"""Explicit installation and read-only checks for build dependencies.

Only setup() downloads anything. Packaging never installs dependencies.
"""

import hashlib
import json
import os
import platform
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

MANIFESTS = Path(__file__).with_name("node_tools")
UNSANDBOXED_CHROMIUM_ENV = "OFFLINE_SNAPSHOT_ALLOW_UNSANDBOXED_CHROMIUM"


def required_executable(name):
    """Return an absolute executable path or a stable setup error."""
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(
            f"{name} is missing. Install Node.js 18 or newer with npm, "
            "then run offline-snapshot setup."
        )
    return executable


def chromium_sandbox_enabled():
    """Keep Chromium sandboxed unless a controlled environment explicitly opts out."""
    return os.environ.get(UNSANDBOXED_CHROMIUM_ENV) != "1"


def browser_launch_options(engine):
    if engine == "chromium":
        return {"chromium_sandbox": chromium_sandbox_enabled()}
    return {}


def cache_directory():
    override = os.environ.get("OFFLINE_SNAPSHOT_CACHE")
    if override:
        base = Path(override).expanduser()
    elif sys.platform == "win32":
        base = (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "offline-snapshot"
        )
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Caches/offline-snapshot"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "offline-snapshot"
    digest = hashlib.sha256((MANIFESTS / "package-lock.json").read_bytes()).hexdigest()[:16]
    return (base / f"node-{platform.system()}-{platform.machine()}-{digest}").resolve()


def parser_environment():
    """Resolve the explicit cache or this source checkout, never the caller's cwd."""
    candidates = [cache_directory()]
    if not os.environ.get("OFFLINE_SNAPSHOT_CACHE"):
        checkout = Path(__file__).resolve().parents[2]
        if (checkout / "src/offline_snapshot/javascript.py").is_file():
            candidates.append(checkout)
    for root in candidates:
        if all(
            (root / "node_modules" / name / "package.json").is_file()
            for name in ("esbuild", "es-module-lexer")
        ):
            return {**os.environ, "_OFFLINE_SNAPSHOT_NODE_ROOT": str(root)}
    raise RuntimeError("JavaScript build tools are missing. Run: offline-snapshot setup")


def check_parser():
    # The executable and packaged script are fixed; no shell is involved.
    result = subprocess.run(  # nosec B603
        [required_executable("node"), str(Path(__file__).with_name("rewrite.cjs"))],
        input=json.dumps(
            [{"id": "probe", "url": "https://example.invalid/", "code": "window.value = 1;"}]
        ),
        text=True,
        capture_output=True,
        timeout=30,
        env=parser_environment(),
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    jobs = json.loads(result.stdout)
    if jobs[0].get("error"):
        raise RuntimeError(jobs[0]["error"])
    return {"status": "passed", "root": parser_environment()["_OFFLINE_SNAPSHOT_NODE_ROOT"]}


def setup(browsers=("chromium", "firefox"), *, progress=None):
    """Install pinned parser dependencies and selected Playwright browsers."""
    node = required_executable("node")
    npm = required_executable("npm")
    # The executable was resolved to an absolute path and the argument is fixed.
    node_version = subprocess.check_output([node, "--version"], text=True)  # nosec B603
    major = int(node_version.strip().lstrip("v").split(".")[0])
    if major < 18:
        raise RuntimeError("Node.js 18 or newer is required.")
    if browsers:
        import importlib.util

        if importlib.util.find_spec("playwright") is None:
            raise RuntimeError(
                'Install the CLI dependencies: python -m pip install "offline-snapshot[cli]"'
            )
    destination = cache_directory()
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        # A complete cache is published only after npm succeeds. Concurrent setup
        # jobs use separate staging directories and can share the finished cache.
        with tempfile.TemporaryDirectory(prefix="node-install-", dir=destination.parent) as temp:
            staging = Path(temp) / "tools"
            staging.mkdir()
            for name in ("package.json", "package-lock.json"):
                shutil.copyfile(MANIFESTS / name, staging / name)
            if progress:
                progress("Installing pinned JavaScript build tools…")
            # npm is an absolute executable and every argument is fixed.
            result = subprocess.run(  # nosec B603
                [npm, "ci", "--no-audit", "--no-fund"],
                cwd=staging,
                stdout=sys.stderr,
                stderr=sys.stderr,
            )
            if result.returncode:
                raise RuntimeError(
                    "npm installation failed; the incomplete cache was discarded. Rerun offline-snapshot setup."
                )
            try:
                staging.rename(destination)
            except OSError:
                if not destination.is_dir():
                    raise
    check_parser()
    if browsers:
        if progress:
            progress("Installing browser binaries…")
        # The current Python runs a fixed module; browser names were validated by the CLI.
        result = subprocess.run(  # nosec B603
            [sys.executable, "-m", "playwright", "install", *browsers],
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        if result.returncode:
            raise RuntimeError(
                "Browser installation failed. Rerun offline-snapshot setup; see Playwright output above for system dependencies."
            )
    return {"status": "installed", "parserDirectory": str(destination), "browsers": list(browsers)}


async def doctor(browsers=("chromium", "firefox")):
    """Check actual parser execution and browser launch without network access."""
    from importlib.metadata import PackageNotFoundError, version

    result = {"python": platform.python_version(), "checks": {}}
    for name in ("lxml", "playwright", "Pillow"):
        try:
            result["checks"][name] = {"status": "passed", "version": version(name)}
        except PackageNotFoundError:
            result["checks"][name] = {"status": "failed", "error": "Install offline-snapshot[cli]"}
    try:
        result["checks"]["parser"] = check_parser()
    except Exception as exc:
        result["checks"]["parser"] = {
            "status": "failed",
            "error": str(exc),
            "action": "Run offline-snapshot setup",
        }
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            for engine in browsers:
                try:
                    options = browser_launch_options(engine)
                    browser = await getattr(pw, engine).launch(**options)
                    try:
                        result["checks"][engine] = {"status": "passed", "version": browser.version}
                        if engine == "chromium":
                            result["checks"][engine]["sandbox"] = options["chromium_sandbox"]
                    finally:
                        await browser.close()
                except Exception as exc:
                    result["checks"][engine] = {"status": "failed", "error": str(exc)}
                    if engine == "chromium":
                        result["checks"][engine]["sandbox"] = chromium_sandbox_enabled()
    except ImportError:
        for engine in browsers:
            result["checks"][engine] = {
                "status": "failed",
                "error": "Install offline-snapshot[cli] and run offline-snapshot setup",
            }
    result["passed"] = all(c["status"] == "passed" for c in result["checks"].values())
    return result

"""Build and verify release artifacts without publishing them."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from email.parser import Parser
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(report, name, command, *, env=None):
    started = time.monotonic()
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, env=env)
    item = {
        "name": name,
        "command": [str(value) for value in command],
        "exitCode": completed.returncode,
        "seconds": round(time.monotonic() - started, 3),
        "stdoutTail": completed.stdout[-5000:],
        "stderrTail": completed.stderr[-5000:],
    }
    report["commands"].append(item)
    print(f"{name}: {completed.returncode} ({item['seconds']}s)", flush=True)
    if completed.returncode:
        for stream in ("stdoutTail", "stderrTail"):
            if item[stream]:
                print(f"--- {name} {stream} ---", file=sys.stderr)
                print(item[stream], file=sys.stderr)
        raise RuntimeError(f"{name} exited {completed.returncode}")


def source_test_environment():
    """Run component tests against this checkout and its npm installation."""
    return {**os.environ, "PYTHONPATH": str(ROOT / "src")}


def inspect_artifacts(dist, version):
    wheels = list(dist.glob("offline_snapshot-*.whl"))
    sdists = list(dist.glob("offline_snapshot-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError("Expected exactly one wheel and one source distribution")
    wheel, sdist = wheels[0], sdists[0]
    with zipfile.ZipFile(wheel) as package:
        members = package.namelist()
        metadata_name = next(name for name in members if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(package.read(metadata_name).decode())
        required = (
            "offline_snapshot/runtime.js",
            "offline_snapshot/bundle_loader.js",
            "offline_snapshot/multipage.js",
            "offline_snapshot/worker_runtime.js",
            "offline_snapshot/rewrite.cjs",
            "offline_snapshot/node_tools/package-lock.json",
        )
        missing = [name for name in required if name not in members]
        if missing:
            raise RuntimeError("Wheel is missing: " + ", ".join(missing))
        if not any(name.endswith(".dist-info/licenses/LICENSE") for name in members):
            raise RuntimeError("Wheel is missing its license")
    if metadata["Version"] != version:
        raise RuntimeError(f"Wheel version {metadata['Version']} != {version}")
    if metadata["Requires-Python"] != ">=3.10":
        raise RuntimeError("Unexpected Requires-Python metadata")
    if metadata["License-Expression"] != "Apache-2.0":
        raise RuntimeError("Unexpected or missing SPDX license expression")
    with tarfile.open(sdist, "r:gz") as package:
        names = package.getnames()
        for suffix in (
            "/LICENSE",
            "/README.md",
            "/RELEASE.md",
            "/SECURITY.md",
            "/scripts/release_check.py",
            "/src/offline_snapshot/runtime.js",
            "/src/offline_snapshot/bundle_loader.js",
        ):
            if not any(name.endswith(suffix) for name in names):
                raise RuntimeError("Source distribution is missing " + suffix[1:])
    return {
        "version": version,
        "wheel": {"path": str(wheel), "bytes": wheel.stat().st_size, "sha256": sha256(wheel)},
        "sdist": {"path": str(sdist), "bytes": sdist.stat().st_size, "sha256": sha256(sdist)},
        "metadata": {
            "name": metadata["Name"],
            "licenseExpression": metadata["License-Expression"],
            "requiresPython": metadata["Requires-Python"],
        },
    }


def wheel_contents(path):
    with zipfile.ZipFile(path) as package:
        return {name: package.read(name) for name in package.namelist()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", default="outputs/release-check-dist")
    parser.add_argument("--report", default="reports/release-check.json")
    parser.add_argument("--phase", default="release-check")
    parser.add_argument(
        "--browser", action="store_true", help="Run the installed-wheel browser acceptance"
    )
    args = parser.parse_args(argv)
    dist = (ROOT / args.dist_dir).resolve()
    report_path = (ROOT / args.report).resolve()
    report = {
        "status": "running",
        "commands": [],
        "artifacts": None,
        "browserAcceptance": args.browser,
    }
    try:
        if dist.exists():
            raise RuntimeError(f"Distribution directory already exists: {dist}")
        dist.mkdir(parents=True)
        run(
            report,
            "python-components",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            env=source_test_environment(),
        )
        run(
            report,
            "javascript-components",
            ["node", "--test", "tests/runtime.test.cjs", "tests/css.test.cjs"],
        )
        try:
            package_version("build")
            build_command = [
                sys.executable,
                "-m",
                "build",
                "--sdist",
                "--wheel",
                "--outdir",
                str(dist),
            ]
            report["buildFrontend"] = "build"
        except PackageNotFoundError:
            uv = shutil.which("uv")
            if not uv:
                raise RuntimeError(
                    "Install the build package or uv to create release artifacts"
                ) from None
            build_command = [uv, "build", "--sdist", "--wheel", "--out-dir", str(dist)]
            report["buildFrontend"] = "uv"
        run(report, "build", build_command)
        from offline_snapshot import __version__

        report["artifacts"] = inspect_artifacts(dist, __version__)
        with tempfile.TemporaryDirectory(prefix="offline-snapshot-sdist-wheel-") as temp:
            sdist = report["artifacts"]["sdist"]["path"]
            if report["buildFrontend"] == "build":
                roundtrip_command = [
                    sys.executable,
                    "-m",
                    "build",
                    "--wheel",
                    "--outdir",
                    temp,
                    sdist,
                ]
            else:
                roundtrip_command = [uv, "build", "--wheel", "--out-dir", temp, sdist]
            run(report, "sdist-wheel", roundtrip_command)
            rebuilt = list(Path(temp).glob("offline_snapshot-*.whl"))
            if len(rebuilt) != 1:
                raise RuntimeError("Source distribution did not produce exactly one wheel")
            if wheel_contents(rebuilt[0]) != wheel_contents(report["artifacts"]["wheel"]["path"]):
                raise RuntimeError("Wheel rebuilt from source distribution has different contents")
            report["artifacts"]["sdistWheelContentsMatch"] = True
        if args.browser:
            command = [
                sys.executable,
                "tests/browser_cli.py",
                "--wheel",
                report["artifacts"]["wheel"]["path"],
                "--phase",
                args.phase,
            ]
            run(report, "installed-wheel-browser", command)
        report["status"] = "passed"
        return_code = 0
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        print("release-check: " + str(exc), file=sys.stderr)
        return_code = 1
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(report_path),
                "artifacts": report["artifacts"],
            },
            indent=2,
        )
    )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())

# Local development

## Setup

Run commands from the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm ci
.venv/bin/python -m playwright install --with-deps chromium firefox
mkdir -p outputs reports
```

Node, esbuild, and es-module-lexer are builder dependencies. Pillow is used for
visual comparison. The generated HTML needs none of them.

`offline-snapshot setup` installs pinned JavaScript tools into a versioned user
cache. `OFFLINE_SNAPSHOT_CACHE` can select another cache directory. Packaging
must never access the network.

## Fast checks

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/bandit -r src -q
.venv/bin/pip-audit . --progress-spinner off
npm audit --audit-level=moderate
.venv/bin/python -m unittest discover -s tests -v
node --test tests/runtime.test.cjs tests/css.test.cjs
```

## Browser acceptance

The clean-checkout acceptance test builds a wheel, installs it outside the
checkout, captures a controlled loopback application, stops its source server,
and validates the file through `file://` in both browsers:

```sh
.venv/bin/python scripts/release_check.py \
  --browser \
  --phase local \
  --dist-dir outputs/release-local \
  --report reports/release-local.json
```

Use a new phase and output directory for each retained run. Exit code 1 means a
test or acceptance failure; exit code 2 from the CLI means setup or command
error. Do not mask either result.

Individual controlled suites live under `tests/browser_*.py`. Select the suite
closest to the changed capability and run it in both engines where supported.
Generated evidence belongs under ignored `outputs/` and `reports/` directories.

Chromium uses its operating-system sandbox by default. The hosted Linux CI runner
cannot provide that sandbox, so only its synthetic loopback test sets
`OFFLINE_SNAPSHOT_ALLOW_UNSANDBOXED_CHROMIUM=1`. Do not use this override with
untrusted content.

## Debugging

Preserve the first failing report and screenshot before changing replay code.
Large HTML files should be parsed with `lxml.html.HTMLParser(huge_tree=True)`.
The embedded Snapshot report lists misses, errors, omissions, and captured scope.

Never substitute unrelated responses, serve the final file over HTTP, fulfill
resources from the harness, disable normal browser security, or count a static
fallback as an interaction pass.

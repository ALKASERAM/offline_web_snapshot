# Contributing

Thank you for improving Offline Snapshot. Keep changes focused, add a regression
test for reproduced bugs, and do not weaken an assertion merely to make a test
pass.

Set up the checkout as described in
[LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md), then run:

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/bandit -r src -q
.venv/bin/pip-audit . --progress-spinner off
npm audit --audit-level=moderate
.venv/bin/python -m unittest discover -s tests -v
node --test tests/runtime.test.cjs tests/css.test.cjs
.venv/bin/python scripts/release_check.py
```

Changes to capture, packing, navigation, CSS rewriting, or browser emulation also
require the relevant controlled browser regression in both Chromium and Firefox.
The source server must be stopped before final offline acceptance.

Do not commit real website captures, generated HTML, screenshots, reports,
credentials, personal browser data, or private response bodies. Use controlled
loopback fixtures with synthetic content. Record the exact command and failure in
the pull request when reporting a browser-specific bug.

Generated website content remains subject to its original ownership and terms.
Only submit fixtures you have permission to redistribute.

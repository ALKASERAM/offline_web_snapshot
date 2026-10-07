# Offline Snapshot

Offline Snapshot captures a website and a bounded set of interactions, then
packages the recorded documents, assets, and responses into one HTML file. The
result opens directly through `file://` in Chromium or Firefox. It needs no
server, browser extension, Python installation, or external replay process.

The capture pipeline accepts arbitrary HTTP and HTTPS URLs. Compatibility is
capability-based rather than tied to a site allowlist. A successful capture
covers only the pages, requests, and interactions that were recorded; it cannot
invent new backend responses for uncaptured searches or transactions.

## Requirements

- Python 3.10 or newer
- Node.js 18 or newer with npm (builder only)
- Chromium and/or Firefox installed by Playwright (capture and validation only)

The generated HTML needs only a supported browser.

## Install

Install the complete CLI from PyPI:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'offline-snapshot[cli]'
.venv/bin/offline-snapshot setup
.venv/bin/offline-snapshot doctor
```

Do not start a capture until `doctor` reports `passed` for at least one browser.
If only Firefox passes, use the Firefox-only command in
[Troubleshooting browser startup](#troubleshooting-browser-startup).

For development from a checkout, see
[LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md).

## Quick start

Capture a URL, follow same-origin links one level deep, build a standalone file,
and validate it offline in Chromium and Firefox:

```sh
offline-snapshot save 'https://example.com/' \
  --depth 1 \
  --max-pages 25 \
  --output outputs/site.html \
  --report-dir reports/site
```

The command writes:

- `outputs/site.capture.json`: the retained recording;
- `outputs/site.html`: the standalone replay file;
- `reports/site/result.json`: capture, build, and validation status;
- per-browser reports and screenshots under `reports/site/`.

Existing files are protected unless `--overwrite` is supplied. Progress is
written to stderr and the final JSON result to stdout.

Exit codes are:

- `0`: the requested operation completed;
- `1`: capture or acceptance was incomplete/failed;
- `2`: invalid input, setup failure, or operational error;
- `130`: cancelled.

An exit code of zero from `pack`, `capture`, or `save --no-validate` is not an
offline browser acceptance result. The JSON status explicitly says when an
artifact was not validated.

## Troubleshooting browser startup

### `Chromium sandboxing failed` on Linux

On Ubuntu 23.10 or newer, AppArmor can prevent Playwright's downloaded Chromium
from creating its sandbox. The error contains `No usable sandbox` or
`Chromium sandboxing failed`. This happens before capture, so `save` does not
write the capture archive or HTML file. It still writes
`reports/<name>/result.json` with the failure details.

The safe immediate fallback is to capture and validate with Firefox:

```sh
.venv/bin/offline-snapshot doctor --browsers firefox

.venv/bin/offline-snapshot save 'https://example.com/' \
  --browser firefox \
  --validation-browsers firefox \
  --depth 1 \
  --max-pages 25 \
  --output outputs/site.html \
  --report-dir reports/site \
  --overwrite
```

`--overwrite` is needed when retrying the same paths because the failed attempt
may already have written `result.json`. Firefox-only validation establishes
Firefox compatibility; it is not evidence that Chromium replay passed.

If Chromium is required, configure a path-specific AppArmor profile or another
supported Chromium sandbox for the Playwright browser executable. Follow the
[Chromium AppArmor guidance](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md).
Do not globally disable the operating-system restriction, and do not use
`OFFLINE_SNAPSHOT_ALLOW_UNSANDBOXED_CHROMIUM=1` to capture public or untrusted
websites. That override is reserved for controlled, trusted test fixtures.

## Capture scope

Useful options include:

| Option | Purpose |
|---|---|
| `--depth N` | Follow same-origin document links by `N` hops. |
| `--max-pages N` | Bound automatic visits, including failures. |
| `--include-path GLOB` | Follow only matching URL paths; repeatable. |
| `--exclude-path GLOB` | Skip matching URL paths; repeatable. |
| `--exclude-resource GLOB` | Omit matching resources and report them. |
| `--asset-limit N` | Bound extra collection of declared static assets. |
| `--scroll-steps N` | Warm lazy content in the main document. |
| `--timeout-ms N` | Bound navigation and action waits. |
| `--settle-timeout-ms N` | Bound request settling at each checkpoint. |
| `--request-timeout-ms N` | Bound response-body and asset reads. |
| `--capture-cookies` | Opt in to capturing script-visible cookie seeds. |

The crawler is breadth-first, keeps query strings significant, deduplicates
cycles, and skips external origins, downloads, credentials, and common account
or mutation paths. It does not submit forms.

Complete public static resources may be reused across crawled pages when their
HTTP cache headers permit it. Documents, API traffic, range responses, private
or expired responses, and request-dependent responses are not reused. Repeated
failed static requests are suppressed after the first failure, but the original
failure and every suppression remain visible in diagnostics.

## SPA interactions

Offline Snapshot supports SPAs within an explicitly recorded scope. You can use
a deterministic recipe or opt in to conservative interaction discovery.

```sh
offline-snapshot save 'https://example.com/app' \
  --discover-interactions \
  --max-actions 20 \
  --exclude-action '.account-menu' \
  --output outputs/app.html \
  --report-dir reports/app
```

Discovery considers visible buttons, tabs, summaries, and button-like same-page
links. It skips forms, downloads, external navigation, disabled or unnamed
controls, and labels that appear account-related or destructive. Non-read HTTP
methods are blocked before reaching the source server. These rules reduce risk;
they do not prove that every GET request or localized label is safe.

For repeatable acceptance, provide a recipe:

```json
{
  "ready": "main",
  "settle_ms": 500,
  "steps": [
    {
      "action": "click",
      "selector": "#details",
      "wait_for": "#details-panel",
      "checkpoint": "details"
    }
  ]
}
```

Supported actions are `click`, `fill`, `press`, `wait`, and `drag`. Assertions
can check selectors, visible text, loaded images, element counts, and routes.
See [recipes/example.json](recipes/example.json) for a starter file.

## Separate commands

The workflow can also be run in stages:

```sh
offline-snapshot capture 'https://example.com/' \
  --output outputs/site.capture.json

offline-snapshot pack outputs/site.capture.json \
  --output outputs/site.html

offline-snapshot validate outputs/site.html \
  --browser both \
  --recipe recipes/example.json \
  --output reports/site-validation.json
```

Packaging is deterministic and does not access the network. Validation opens the
actual file through `file://` in a fresh browser context, disables networking,
and does not serve or fulfill resources from the test harness.

## What is recorded

Captured responses are matched by method, canonical URL, exact request body, and
Range header. Repeated responses preserve their order. Missing requests fail
locally instead of receiving an unrelated response.

The recorder also discovers declared images, stylesheets, fonts, media sources,
posters, and icons that the active browser did not request. Such HTTP collection
is labeled separately from browser-observed traffic and remains subject to
resource, size, timeout, and exclusion limits.

Packing deduplicates equal response bodies, shares static resources between
captured documents, compresses multipage bundles, and defers eligible asynchronous
API bodies until first use. The output remains one self-contained HTML file.

## Privacy and trust

Capture archives and generated HTML can contain page text, URLs, response bodies,
request bodies, and application data. Script-visible cookies are omitted unless
`--capture-cookies` is explicitly supplied. Authorization headers, `Set-Cookie`,
and HttpOnly cookies are not persisted by the recorder.

Run the value-free privacy audit before sharing an artifact:

```sh
offline-snapshot inspect outputs/site.capture.json
offline-snapshot inspect outputs/site.capture.json --fail-on-sensitive
```

The audit is heuristic. Generated HTML contains captured third-party code and is
not a security sandbox. Open untrusted captures in an isolated browser profile or
disposable environment. See [SECURITY.md](SECURITY.md).

## Current limitations

- Only recorded responses can be replayed; arbitrary new backend operations are
  outside the capture.
- Automatic discovery is bounded and follows one evolving interaction path.
- IndexedDB, WebSocket, EventSource, service workers, module/shared/nested
  workers, and worker XHR are not fully replayed.
- Dynamic code and unusual CSS or DOM mutation can bypass rewriting.
- Map, 3D, deep-zoom, and streaming-media experiences require enough resources
  and explicit interaction coverage; compatibility is not guaranteed.
- Responsive resources reflect the captured viewport and observed choices.
- Build and browser acceptance are currently enforced on Linux; other builder
  platforms are best effort.

Warnings, omissions, unsupported APIs, network attempts, and missing replay
requests remain visible in the capture and validation reports. Static fallback
views do not count as interaction acceptance.

## Development and releases

Contribution rules are in [CONTRIBUTING.md](CONTRIBUTING.md). The test matrix and
local workflow are in [LOCAL_DEVELOPMENT.md](LOCAL_DEVELOPMENT.md). Release
criteria and package verification are in [RELEASE.md](RELEASE.md), with the
validation model described in [VALIDATION.md](VALIDATION.md).

Offline Snapshot is licensed under the Apache License 2.0.

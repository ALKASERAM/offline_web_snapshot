# Changelog

All notable changes are recorded here. The project follows semantic versioning
from the first stable release.

## Unreleased

## 2.0.0 — 2026-10-07

- Remove the legacy site adapter and all site-specific runtime transformations;
  capture, packaging, and replay now use one capability-based pipeline.
- Remove private benchmark artifacts, recipes, editor configuration, and
  historical reports from the distributable repository.
- Replace local-file-dependent checks with controlled, self-contained release
  fixtures.
- Add enforced Ruff linting and formatting, complete package metadata, and a
  protected Trusted Publishing workflow for PyPI.
- Refresh public documentation around scope, privacy, validation, development,
  and release responsibilities.

## 1.3.1 — 2026-10-07

- Reuse complete static GET responses across crawled pages only when standard
  HTTP headers explicitly mark them public and fresh. Request-dependent,
  expired, ranged, dynamic API and non-GET responses remain uncached.
- Stop retrying the same unexcluded failed static URL on every later crawled
  page. The first failure still makes the capture incomplete, and every locally
  suppressed repeat remains visible in `captureDiagnostics`.
- Avoid adding repeated cache-fulfilled static bodies to the capture archive.
- Add Chromium and Firefox regressions for both optimizations without relying
  on a site-specific adapter.

## 1.3.0 — 2026-10-06

- Keep response bodies used exclusively by `fetch` or observed asynchronous XHR
  in separate deterministic gzip chunks and decompress them only when requested.
- Retain static resources, scripts, workers and synchronous, mixed or unknown
  XHR bodies eagerly so existing synchronous browser behavior is preserved.
- Bound the decompressed chunk cache and pass large-body offline replay in
  Chromium and Firefox with eviction and zero network access.
- Add a reproducible Linux renderer-memory benchmark. On the controlled 88 MiB
  API fixture, median startup peak deltas fall 82.2% in Chromium and 64.0% in
  Firefox while all measured opens remain clean.
- Run source component tests against the checkout explicitly so fresh CI runners
  use the parser dependencies installed by the workflow instead of requiring an
  existing Offline Snapshot tool cache.
- Print captured subprocess output tails when the release gate fails.
- Keep Chromium sandboxing enabled by default while allowing the controlled
  GitHub-hosted browser gate to declare and record its required sandbox opt-out.

## 1.2.0 — 2026-10-06

- Store generic multipage replay bundles as deterministic embedded gzip data,
  reducing a preserved 35.6 MB benchmark to 16.8 MB.
- Decode the bundle outside the JavaScript parser and transfer archive state to
  replay documents without regenerating a large JavaScript bootstrap.
- Keep response-body base64 decoding on demand and show an explicit loading or
  startup-error state while the self-contained viewer initializes.
- Add a reproducible offline Chromium/Firefox HTML performance benchmark.

## 1.1.0 — 2026-10-06

- Add opt-in discovery of safe, non-form SPA controls and automatically verify
  the exact recorded sequence during `save`.
- Exclude forms, destructive/account-labelled controls, downloads, external
  navigation and user-selected CSS scopes from automatic interaction attempts.
- Block non-read requests before they reach the server and stop discovery when
  a blocked attempt may have changed client state.
- Record activated, skipped, failed and blocked controls with bounded limits and
  explicit safety limitations.

## 1.0.0 — 2026-10-05

- Separate capture, build and verification stage statuses.
- Treat unexcluded browser failures and missing declared scripts/stylesheets as
  incomplete capture evidence.
- Add configurable offline validation timeouts.
- Reliably exclude the replay diagnostic panel from visual comparisons inside
  opaque-origin multipage frames.
- Deduplicate static resources across compiled documents and frames.
- Deduplicate equal response bodies without collapsing response ordering.
- Reduce large-pack memory use with mutation-safe structural copies and direct
  UTF-8 output.
- Add value-free privacy audits and an `inspect` release gate.
- Make script-visible cookie capture explicit opt-in.
- Add public licensing, security and contribution documentation.

## 0.2.4 — 2026-09-19

- Retain depth-zero recordings when an asynchronous subresource prevents the
  source document from reaching `readyState=complete`.
- Preserve timeout and pending-request evidence while keeping strict acceptance
  incomplete.

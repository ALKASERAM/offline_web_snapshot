# Security policy

## Supported versions

Security fixes are made on the latest released minor version. Older generated
artifacts and captured websites are evidence, not supported software releases.

## Reporting a vulnerability

Use the repository host's private security-reporting channel when one is
available. Otherwise contact the maintainers through the private contact method
listed with the release. Do not attach a real capture, authentication token,
cookie or private response body. Provide a minimal controlled reproduction and
the `offline-snapshot --version`, Python, Playwright and browser versions.

## Trust model

An archive and its generated HTML contain captured third-party content. The HTML
runs captured scripts in a restrictive offline replay environment, but it is not
a security sandbox for hostile code. Open untrusted captures in an isolated
browser profile or disposable environment.

Packaging never accesses the network. Validation opens the actual `file://`
artifact in a fresh offline browser context and aborts external requests. A
capture can still contain sensitive page text, URLs, request/response bodies and,
when explicitly enabled, script-visible cookie seeds. Run `offline-snapshot
inspect CAPTURE.json --fail-on-sensitive` and review the content owner and scope
before sharing either the archive or generated HTML.

Chromium is launched with its operating-system sandbox enabled by default.
GitHub-hosted Ubuntu runners do not provide a usable Chromium sandbox, so the CI
workflow explicitly sets `OFFLINE_SNAPSHOT_ALLOW_UNSANDBOXED_CHROMIUM=1` only for
its controlled loopback fixtures. That opt-out is recorded in capture and
validation environment diagnostics. Do not set it when capturing or opening
untrusted content; use a host that supports Chromium's sandbox instead.

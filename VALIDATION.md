# Validation model

Offline Snapshot distinguishes component correctness, capture completeness,
packaging success, and browser acceptance. None is a substitute for another.

## Required release evidence

1. Python and JavaScript component tests pass from a clean checkout.
2. Wheel and source distribution build successfully.
3. A wheel rebuilt from the source distribution matches the direct wheel.
4. The installed wheel captures a controlled multipage application.
5. The source server is stopped before replay validation.
6. The generated HTML passes through `file://` in Chromium and Firefox with
   external networking aborted.
7. Reports contain no unexpected missing requests, network attempts, page
   errors, console errors, or failed assertions.

Visual references must come from the same browser, viewport, locale, and
interaction state. A visual comparison is evidence for the tested checkpoints,
not proof of compatibility at every size or for unrecorded behavior.

## Status vocabulary

- **passed**: the stated assertion ran and succeeded;
- **failed**: it ran and produced contrary evidence;
- **blocked**: environment or external state prevented execution;
- **not run**: no evidence was collected.

Warnings and failed crawl visits prevent a complete capture from being reported
as passed. Explicit resource omissions remain visible even when intentional.
Early validation failures must still retain diagnostics and a screenshot whenever
the browser remains responsive.

## Manual release check

After the automated gate, open the final HTML in a fresh desktop browser while
offline. Confirm initial content, image completion, recorded interactions,
captured navigation, the diagnostic panel, and return paths. Record the browser
versions and exact artifact hash with the release evidence.

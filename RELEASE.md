# Release process

Offline Snapshot follows semantic versioning. The stable public API consists of
the documented CLI and `offline_snapshot.workflow.save`. Lower-level modules and
additive capture metadata may evolve between minor releases.

## Pre-release checklist

1. Work from a clean checkout and review every tracked file.
2. Confirm that generated captures, reports, credentials, and editor state are
   not tracked.
3. Update the version in `pyproject.toml` and `src/offline_snapshot/__init__.py`.
4. Add the release notes to `CHANGELOG.md`.
5. Run the complete release gate:

```sh
.venv/bin/python scripts/release_check.py \
  --browser \
  --phase release-X.Y.Z \
  --dist-dir outputs/release-X.Y.Z \
  --report reports/release-X.Y.Z.json
```

6. Review the wheel metadata, source distribution contents, hashes, and browser
   reports.
7. Open the built HTML manually in a fresh offline desktop browser.
8. Commit the release, create an annotated `vX.Y.Z` tag, and push the commit and
   tag only after the tree is clean.

## Publishing to PyPI

Publishing is performed by the `publish.yml` workflow after a GitHub Release is
created for a version tag. The PyPI project should use Trusted Publishing for
this repository and workflow; no long-lived API token belongs in the repository.

The workflow builds distributions again, runs package verification, and publishes
from a protected `pypi` environment. TestPyPI should be used for the first public
release or after packaging changes.

## Acceptance boundary

A passing release gate proves the controlled capabilities exercised by the test
suite. It does not promise that arbitrary uncaptured routes, transactions,
queries, media streams, or browser APIs work offline. The package must preserve
missing-resource and unsupported-capability evidence rather than fabricating
success.

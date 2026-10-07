import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from offline_snapshot import __version__
from offline_snapshot.toolchain import browser_launch_options, required_executable
from scripts.release_check import source_test_environment

ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_stable_version_is_consistent_and_changelog_has_release(self):
        project = (ROOT / "pyproject.toml").read_text()
        version = re.search(r'^version\s*=\s*"([^"]+)"', project, re.M).group(1)
        self.assertEqual(version, __version__)
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertIn("Development Status :: 5 - Production/Stable", project)
        self.assertIn(f"## {version} ", (ROOT / "CHANGELOG.md").read_text())

    def test_bundled_node_manifests_match_builder_manifests(self):
        for name in ("package.json", "package-lock.json"):
            root = json.loads((ROOT / name).read_text())
            bundled = json.loads((ROOT / "src/offline_snapshot/node_tools" / name).read_text())
            self.assertEqual(root, bundled)

    def test_public_release_documents_exist(self):
        for name in (
            "LICENSE",
            "README.md",
            "RELEASE.md",
            "SECURITY.md",
            "CONTRIBUTING.md",
            "CHANGELOG.md",
        ):
            self.assertGreater((ROOT / name).stat().st_size, 100)

    def test_release_gate_components_import_this_checkout(self):
        with_source = source_test_environment()
        self.assertEqual(Path(with_source["PYTHONPATH"]).resolve(), ROOT / "src")
        self.assertEqual(
            {key: value for key, value in with_source.items() if key not in {"PYTHONPATH"}},
            {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH"}},
        )

    def test_chromium_sandbox_requires_explicit_opt_out(self):
        variable = "OFFLINE_SNAPSHOT_ALLOW_UNSANDBOXED_CHROMIUM"
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(browser_launch_options("chromium"), {"chromium_sandbox": True})
            self.assertEqual(browser_launch_options("firefox"), {})
        with patch.dict(os.environ, {variable: "1"}, clear=True):
            self.assertEqual(browser_launch_options("chromium"), {"chromium_sandbox": False})

    def test_build_tools_resolve_to_absolute_executables(self):
        with patch("offline_snapshot.toolchain.shutil.which", return_value="/tools/bin/node"):
            self.assertEqual(required_executable("node"), "/tools/bin/node")
        with patch("offline_snapshot.toolchain.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "node is missing"):
                required_executable("node")


if __name__ == "__main__":
    unittest.main()

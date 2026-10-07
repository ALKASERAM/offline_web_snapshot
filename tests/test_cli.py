import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from offline_snapshot.archive import entry, new_archive
from offline_snapshot.archive import save as save_archive
from offline_snapshot.cli import main
from offline_snapshot.readiness import ReadinessError, read_recipe
from offline_snapshot.workflow import save


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "html/site.html"
        self.archive = self.root / "recordings/site.json"
        self.reports = self.root / "evidence"

    async def recorded(self, url, output, **kwargs):
        archive = new_archive(url)
        archive["entries"] = [
            entry(url, b"<!doctype html><html><body>Recorded page</body></html>", "text/html")
        ]
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        save_archive(archive, output)
        return {"warnings": [], "crawl": {"pages": []}, "screenshots": str(self.reports / "online")}

    async def run_save(self, **kwargs):
        return await save(
            "https://example.test/",
            self.output,
            archive_path=self.archive,
            report_dir=self.reports,
            **kwargs,
        )

    async def test_browser_failure_runs_second_browser_and_keeps_artifacts(self):
        failed = {"passed": False, "checks": [{"passed": False}], "visualChecks": []}
        passed = {"passed": True, "checks": [{"passed": True}]}
        with (
            patch("offline_snapshot.workflow.capture", side_effect=self.recorded),
            patch(
                "offline_snapshot.workflow.validate", new=AsyncMock(side_effect=[failed, passed])
            ) as validate,
        ):
            result = await self.run_save()
        self.assertEqual(result["status"], "failed")
        self.assertEqual([c.args[1] for c in validate.call_args_list], ["chromium", "firefox"])
        self.assertIsNotNone(validate.call_args_list[0].kwargs["visual_baseline"])
        self.assertIsNone(validate.call_args_list[1].kwargs["visual_baseline"])
        self.assertTrue(self.output.is_file())
        self.assertTrue(self.archive.is_file())
        self.assertEqual(json.loads((self.reports / "result.json").read_text())["status"], "failed")
        self.assertEqual(result["stages"]["capture"]["status"], "complete")
        self.assertEqual(result["stages"]["build"]["status"], "built")
        self.assertEqual(result["stages"]["verification"]["status"], "failed")

    async def test_browser_setup_error_still_runs_other_engine(self):
        with (
            patch("offline_snapshot.workflow.capture", side_effect=self.recorded),
            patch(
                "offline_snapshot.workflow.validate",
                new=AsyncMock(side_effect=[RuntimeError("Missing browser"), {"passed": True}]),
            ) as validate,
        ):
            result = await self.run_save()
        self.assertEqual(result["status"], "error")
        self.assertEqual(validate.await_count, 2)
        self.assertEqual(
            json.loads((self.reports / "chromium.json").read_text())["setupError"],
            "Missing browser",
        )

    async def test_pack_error_preserves_recording_and_writes_stage(self):
        with (
            patch("offline_snapshot.workflow.capture", side_effect=self.recorded),
            patch("offline_snapshot.workflow.pack", side_effect=RuntimeError("Parser missing")),
        ):
            with self.assertRaisesRegex(RuntimeError, "Parser missing"):
                await self.run_save()
        self.assertTrue(self.archive.is_file())
        self.assertEqual(json.loads((self.reports / "result.json").read_text())["stage"], "pack")

    async def test_no_validate_never_claims_a_pass(self):
        with (
            patch("offline_snapshot.workflow.capture", side_effect=self.recorded),
            patch("offline_snapshot.workflow.validate") as validate,
        ):
            result = await self.run_save(verify=False)
        self.assertEqual(result["status"], "not_validated")
        self.assertFalse(result["passed"])
        self.assertEqual(result["stages"]["verification"]["status"], "not_run")
        validate.assert_not_called()

    async def test_validation_timeout_is_forwarded(self):
        with (
            patch("offline_snapshot.workflow.capture", side_effect=self.recorded),
            patch(
                "offline_snapshot.workflow.validate", new=AsyncMock(return_value={"passed": True})
            ) as validate,
        ):
            result = await self.run_save(validation_timeout=42000, browsers=("chromium",))
        self.assertTrue(result["passed"])
        self.assertEqual(validate.call_args.kwargs["timeout"], 42000)

    async def test_discovered_steps_extend_validation_without_mutating_recipe(self):
        recipe = self.root / "recipe.json"
        recipe.write_text(
            json.dumps(
                {
                    "steps": [
                        {
                            "action": "click",
                            "selector": "#manual",
                            "checkpoint": "manual",
                        }
                    ]
                }
            )
        )

        async def discovered(url, output, **kwargs):
            result = await self.recorded(url, output, **kwargs)
            result["interactionDiscovery"] = {
                "steps": [
                    {
                        "action": "click",
                        "selector": "#automatic",
                        "checkpoint": "discovered-1",
                    }
                ]
            }
            return result

        with (
            patch("offline_snapshot.workflow.capture", side_effect=discovered),
            patch(
                "offline_snapshot.workflow.validate", new=AsyncMock(return_value={"passed": True})
            ) as validate,
        ):
            result = await self.run_save(
                recipe_path=recipe, browsers=("chromium",), discover_interactions=True
            )
        used = validate.call_args.args[2]
        self.assertEqual([step["selector"] for step in used["steps"]], ["#manual", "#automatic"])
        self.assertEqual(len(json.loads(recipe.read_text())["steps"]), 1)
        self.assertIn("1 automatically discovered", result["verificationScope"]["interactions"])

    async def test_incomplete_capture_cannot_pass_even_if_browser_checks_pass(self):
        async def incomplete(*args, **kwargs):
            result = await self.recorded(*args, **kwargs)
            result["crawl"]["pages"] = [{"url": "https://example.test/missing", "status": "failed"}]
            return result

        with (
            patch("offline_snapshot.workflow.capture", side_effect=incomplete),
            patch(
                "offline_snapshot.workflow.validate", new=AsyncMock(return_value={"passed": True})
            ),
        ):
            result = await self.run_save()
        self.assertFalse(result["captureComplete"])
        self.assertEqual(result["status"], "failed")

    async def test_existing_files_and_invalid_url_fail_before_capture(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_text("preserved")
        with patch("offline_snapshot.workflow.capture") as capture:
            with self.assertRaises(FileExistsError):
                await self.run_save()
            with self.assertRaises(ValueError):
                await save("file:///etc/passwd", self.output)
            with self.assertRaises(ValueError):
                await save("https://user:password@example.test/", self.output)
            capture.assert_not_called()
        self.assertEqual(self.output.read_text(), "preserved")

    async def test_failed_source_readiness_never_builds_or_validates(self):
        error = ReadinessError(
            {
                "selector": "#content",
                "status": "failed",
                "error": "Overlay intercepts pointer events",
            }
        )
        with (
            patch("offline_snapshot.workflow.capture", new=AsyncMock(side_effect=error)),
            patch("offline_snapshot.workflow.pack") as pack,
            patch("offline_snapshot.workflow.validate") as validate,
        ):
            with self.assertRaises(ReadinessError):
                await self.run_save(ready_selector="#content")
            pack.assert_not_called()
            validate.assert_not_called()
        report = json.loads((self.reports / "result.json").read_text())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["stage"], "source_readiness")
        self.assertFalse(self.output.exists())


class CliTests(unittest.TestCase):
    def test_capture_limits_and_progress_are_forwarded(self):
        result = {"warnings": ["Page did not settle"], "crawl": {"pages": []}}
        with (
            patch(
                "offline_snapshot.capture.capture", new=AsyncMock(return_value=result)
            ) as capture,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = main(
                [
                    "capture",
                    "https://example.test/",
                    "-o",
                    "outputs/not-created.capture.json",
                    "--settle-timeout-ms",
                    "1000",
                    "--request-timeout-ms",
                    "3000",
                ]
            )
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stdout.getvalue())["captureComplete"])
        self.assertEqual(capture.call_args.kwargs["settle_timeout"], 1000)
        self.assertEqual(capture.call_args.kwargs["request_timeout"], 3000)
        self.assertTrue(callable(capture.call_args.kwargs["progress"]))

    def test_cookie_capture_requires_explicit_opt_in(self):
        result = {
            "warnings": [],
            "crawl": {"pages": []},
            "captureComplete": True,
            "status": "recorded",
        }
        with (
            patch(
                "offline_snapshot.capture.capture", new=AsyncMock(return_value=result)
            ) as capture,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["capture", "https://example.test/", "-o", "unused.json"]), 0)
            self.assertFalse(capture.call_args.kwargs["capture_cookies"])
        with (
            patch(
                "offline_snapshot.capture.capture", new=AsyncMock(return_value=result)
            ) as capture,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                main(
                    ["capture", "https://example.test/", "-o", "unused.json", "--capture-cookies"]
                ),
                0,
            )
            self.assertTrue(capture.call_args.kwargs["capture_cookies"])

    def test_interaction_discovery_options_are_forwarded(self):
        result = {
            "warnings": [],
            "crawl": {"pages": []},
            "captureComplete": True,
            "status": "recorded",
        }
        args = [
            "capture",
            "https://example.test/",
            "-o",
            "unused.json",
            "--discover-interactions",
            "--max-actions",
            "7",
            "--include-action",
            "[role=tab]",
            "--exclude-action",
            ".danger",
        ]
        with (
            patch(
                "offline_snapshot.capture.capture", new=AsyncMock(return_value=result)
            ) as capture,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(args), 0)
        options = capture.call_args.kwargs
        self.assertTrue(options["discover_interactions"])
        self.assertEqual(options["max_actions"], 7)
        self.assertEqual(options["include_actions"], ["[role=tab]"])
        self.assertEqual(options["exclude_actions"], [".danger"])

    def test_cli_readiness_override_is_forwarded_and_failure_exits_one(self):
        error = ReadinessError({"selector": "#content", "status": "failed", "error": "Covered"})
        with (
            patch("offline_snapshot.cli.save", new=AsyncMock(side_effect=error)) as save,
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                main(
                    [
                        "save",
                        "https://example.test/",
                        "-o",
                        "site.html",
                        "--ready-selector",
                        "#content",
                    ]
                ),
                1,
            )
            self.assertEqual(save.call_args.kwargs["ready_selector"], "#content")
            self.assertEqual(json.loads(stdout.getvalue())["stage"], "source_readiness")

    def test_recipe_override_does_not_modify_recipe_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "recipe.json"
            source = '{"ready":"main","expect_text":"Grüße"}'
            path.write_text(source, encoding="utf-8")
            self.assertEqual(
                read_recipe(path, "#content"), {"ready": "#content", "expect_text": "Grüße"}
            )
            self.assertEqual(path.read_text(encoding="utf-8"), source)

    def test_dictionary_recipe_is_copied(self):
        source = {"ready": "main", "steps": []}
        result = read_recipe(source, "#content")
        result["steps"].append({"action": "click"})
        self.assertEqual(source, {"ready": "main", "steps": []})

    def test_validation_report_cannot_overwrite_input_html(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "page.html"
            path.write_text("preserved")
            with (
                patch("offline_snapshot.capture.validate") as validate,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(["validate", str(path), "-o", str(path)]), 2)
                validate.assert_not_called()
            self.assertEqual(path.read_text(), "preserved")

    def test_validate_timeout_is_forwarded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "page.html"
            path.write_text("<!doctype html><title>test</title>")
            with (
                patch(
                    "offline_snapshot.capture.validate",
                    new=AsyncMock(return_value={"passed": True}),
                ) as validate,
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    main(["validate", str(path), "--validation-timeout-ms", "42000"]), 0
                )
            self.assertEqual(validate.call_args.kwargs["timeout"], 42000)

    def test_save_failure_and_setup_error_have_distinct_exit_codes(self):
        for status, expected in [("passed", 0), ("failed", 1), ("error", 2), ("not_validated", 0)]:
            with (
                self.subTest(status=status),
                patch("offline_snapshot.cli.save", new=AsyncMock(return_value={"status": status})),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(
                    main(["save", "https://example.test/", "-o", "outputs/site.html"]), expected
                )
                self.assertEqual(json.loads(stdout.getvalue())["status"], status)

    def test_numeric_and_url_errors_are_rejected_before_work(self):
        for args in [
            ("--depth", "-1"),
            ("--max-pages", "0"),
            ("--max-visual-change", "nan"),
            ("--timeout-ms", "0"),
            ("--settle-timeout-ms", "0"),
            ("--request-timeout-ms", "-1"),
            ("--validation-timeout-ms", "0"),
        ]:
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(["save", "https://example.test/", "-o", "out.html", *args])
                self.assertEqual(error.exception.code, 2)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(["capture", "https://user:password@example.test/", "-o", "out.json"])
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

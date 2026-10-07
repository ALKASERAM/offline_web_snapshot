import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from offline_snapshot.archive import entry, new_archive, privacy_audit, save
from offline_snapshot.cli import main


class PrivacyTests(unittest.TestCase):
    def sensitive_archive(self):
        archive = new_archive("https://example.test/?access_token=do-not-report-this")
        record = entry(
            archive["url"],
            b'{"email":"private@example.test"}',
            mime="application/json",
            method="POST",
            request_body=b'{"password":"also-private"}',
            headers={"content-type": "application/json", "x-api-key": "header-secret"},
        )
        record["resourceType"] = "fetch"
        archive["entries"] = [record]
        archive["pages"] = [
            {"url": archive["url"], "cookieSeed": [{"name": "sessionid", "value": "cookie-secret"}]}
        ]
        archive["storage"] = {"localStorage": {"account": "storage-secret"}, "sessionStorage": {}}
        return archive

    def test_audit_reports_names_and_counts_without_values(self):
        report = privacy_audit(self.sensitive_archive())
        encoded = json.dumps(report)
        self.assertTrue(report["containsPotentiallySensitiveData"])
        self.assertEqual(
            report["sensitiveQueryParameters"], [{"name": "access_token", "occurrences": 1}]
        )
        self.assertEqual(report["requestBodies"]["sensitiveFieldNames"], ["password"])
        self.assertEqual(report["cookieSeeds"]["names"], ["sessionid"])
        self.assertEqual(report["sensitiveResponseHeaderNames"], ["x-api-key"])
        for secret in (
            "do-not-report-this",
            "also-private",
            "private@example.test",
            "cookie-secret",
            "storage-secret",
            "header-secret",
        ):
            self.assertNotIn(secret, encoded)

    def test_static_public_archive_has_no_obvious_sensitive_state(self):
        archive = new_archive("https://example.test/")
        archive["entries"] = [entry(archive["url"], b"<html>Public</html>", mime="text/html")]
        report = privacy_audit(archive)
        self.assertFalse(report["containsPotentiallySensitiveData"])
        self.assertEqual(report["status"], "no_obvious_sensitive_state")

    def test_inspect_can_fail_a_release_gate_without_overwriting_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "capture.json"
            save(self.sensitive_archive(), path)
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(["inspect", str(path), "--fail-on-sensitive"]), 1)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["status"], "failed")
            self.assertTrue(path.is_file())
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(main(["inspect", str(path), "-o", str(path)]), 2)


if __name__ == "__main__":
    unittest.main()

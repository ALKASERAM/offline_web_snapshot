import asyncio
import unittest
from typing import ClassVar

from offline_snapshot.archive import decode, entry, new_archive
from offline_snapshot.javascript import check_script_sources
from offline_snapshot.script_integrity import complete_scripts, script_record


class Response:
    status = 200
    url = "https://example.test/app.js"
    headers: ClassVar[dict[str, str]] = {
        "content-type": "application/javascript",
        "etag": '"v1"',
    }

    def __init__(self):
        self.disposed = False

    async def body(self):
        return b'window.value="complete";'

    async def dispose(self):
        self.disposed = True


class RequestContext:
    def __init__(self, response=None):
        self.response = response or Response()
        self.calls = []

    async def get(self, url, **options):
        self.calls.append((url, options))
        return self.response


class ScriptIntegrityTests(unittest.IsolatedAsyncioTestCase):
    def archive(self):
        a = new_archive("https://example.test/")
        a["entries"] = [
            entry(
                "https://example.test/app.js",
                b'window.value="com',
                "application/javascript",
                headers={"content-type": "application/javascript", "etag": '"v1"'},
            )
        ]
        return a

    def test_syntax_check_does_not_execute_and_accepts_modules(self):
        result = check_script_sources(
            [
                'throw new Error("must not execute")',
                'import value from "./a.js"; export default value;',
                'window.value="unterminated',
            ]
        )
        self.assertEqual(result[:2], [None, None])
        self.assertIn("Unterminated string", result[2])

    def test_only_complete_static_get_scripts_qualify(self):
        source = self.archive()["entries"][0]
        self.assertTrue(script_record(source))
        for changes in [
            {"method": "POST"},
            {"status": 206},
            {"requestBody": "eA=="},
            {"requestHeaders": {"range": "bytes=0-10"}},
            {"mime": "application/json"},
        ]:
            self.assertFalse(script_record({**source, **changes}), changes)

    async def test_redirect_cannot_bypass_exclusion(self):
        a = self.archive()
        response = Response()
        response.status = 302
        response.headers = {"location": "https://excluded.test/app.js"}
        context = RequestContext(response)
        await complete_scripts(
            a, context, timeout=1000, exclusions=["https://excluded.test/*"], total=20
        )
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(context.calls[0][1]["headers"], {"if-match": '"v1"'})
        self.assertTrue(response.disposed)
        self.assertIn("excluded", a["scriptIntegrity"]["failures"][0]["error"])
        self.assertEqual(decode(a["entries"][0]), b'window.value="com')

    async def test_missing_version_validator_never_refetches(self):
        a = self.archive()
        a["entries"][0]["headers"].pop("etag")
        context = RequestContext()
        await complete_scripts(a, context, timeout=1000, exclusions=[], total=20)
        self.assertEqual(context.calls, [])
        self.assertTrue(a["scriptIntegrity"]["failures"])

    async def test_completion_respects_total_budget_and_keeps_original(self):
        a = self.archive()
        context = RequestContext()
        await complete_scripts(a, context, timeout=1000, exclusions=[], total=150 * 1024 * 1024)
        self.assertIn("budget", a["scriptIntegrity"]["failures"][0]["error"])
        self.assertFalse(a["scriptIntegrity"]["recoveries"])
        self.assertEqual(decode(a["entries"][0]), b'window.value="com')
        self.assertTrue(context.response.disposed)

    async def test_redirects_share_one_completion_deadline(self):
        a = self.archive()

        class SlowRedirect(RequestContext):
            async def get(self, url, **options):
                self.calls.append((url, options))
                await asyncio.sleep(0.04)
                self.response.status = 302
                self.response.headers = {"location": "https://example.test/next.js"}
                return self.response

        context = SlowRedirect()
        await complete_scripts(a, context, timeout=20, exclusions=[], total=20)
        self.assertEqual(len(context.calls), 1)
        self.assertIn("timeout", a["scriptIntegrity"]["failures"][0]["error"])
        self.assertTrue(context.response.disposed)
        self.assertEqual(decode(a["entries"][0]), b'window.value="com')

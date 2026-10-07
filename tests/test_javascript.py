import json
import subprocess
import unittest

from offline_snapshot.archive import decode, entry, new_archive
from offline_snapshot.javascript import prepare_scripts


class JavaScriptTests(unittest.TestCase):
    def test_location_rewrite_preserves_shadowed_bindings_and_strings(self):
        archive = new_archive("https://example.test/")
        archive["entries"] = [
            entry(
                "https://example.test/app.js",
                b"""
        function local(location) { return location.pathname; }
        window.result=[window.location.pathname,location.pathname,local({pathname:'local'}),'window.location'];
        """,
                "application/javascript",
            )
        ]
        prepare_scripts(archive)
        runner = "const vm=require('node:vm'),fs=require('node:fs');const context={window:{},__offlineLocation:{pathname:'/recorded'}};context.__offlineWindow=context.window;vm.runInNewContext(fs.readFileSync(0,'utf8'),context);process.stdout.write(JSON.stringify(context.window.result));"
        result = subprocess.run(
            ["node", "-e", runner],
            input=decode(archive["entries"][0]).decode(),
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(
            json.loads(result.stdout), ["/recorded", "/recorded", "local", "window.location"]
        )

    def test_empty_html_beacon_is_retained_without_document_parsing(self):
        archive = new_archive("https://example.test/")
        archive["entries"] = [
            entry(archive["url"], b"<html><body>Page</body></html>", "text/html"),
            entry("https://tracker.test/beacon", b"", "text/html"),
        ]
        prepare_scripts(archive)
        self.assertEqual(decode(archive["entries"][1]), b"")
        self.assertNotIn("https://tracker.test/beacon", archive["documentSettings"])

    def test_module_imports_keep_original_url_and_import_map(self):
        archive = new_archive("https://example.test/page")
        archive["entries"] = [
            entry(
                archive["url"],
                b'<html><head><script type="importmap">{"imports":{"alias":"/module.js"}}</script><script type="module">import x from "./module.js"; window.url=import.meta.url;</script></head></html>',
                "text/html",
            )
        ]
        prepare_scripts(archive)
        self.assertEqual(
            archive["documentSettings"][archive["url"]]["importMap"]["imports"],
            {"alias": "/module.js"},
        )
        rewritten = decode(archive["entries"][0]).decode()
        self.assertIn("https://example.test/module.js", rewritten)
        self.assertIn("https://example.test/page", rewritten)
        self.assertNotIn("import.meta.url", rewritten)

    def test_html_api_fragment_keeps_exact_body(self):
        archive = new_archive("https://example.test/")
        fragment = b'<span class="result">A captured API fragment</span>'
        archive["entries"] = [
            entry(archive["url"], b"<html><body>Page</body></html>", "text/html"),
            entry("https://example.test/template", fragment, "text/html"),
        ]
        prepare_scripts(archive)
        self.assertEqual(decode(archive["entries"][1]), fragment)

    def test_document_http_charset_survives_utf8_serialization(self):
        from lxml import html

        archive = new_archive("https://example.test/")
        text = "Grüße\u00a0世界"
        archive["entries"] = [
            entry(archive["url"], f"<p>{text}</p>".encode(), "text/html; charset=UTF-8")
        ]
        prepare_scripts(archive)
        document = html.document_fromstring(decode(archive["entries"][0]))
        self.assertEqual(document.find(".//p").text, text)


if __name__ == "__main__":
    unittest.main()

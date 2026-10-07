import base64
import copy
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from lxml import html

from offline_snapshot.archive import canonical_url, entry, new_archive
from offline_snapshot.pack import pack, script_json


class PackagingTests(unittest.TestCase):
    def packed_bundle(self, source):
        root = html.document_fromstring(source)
        payload = root.get_element_by_id("offline-snapshot-bundle").text
        return json.loads(gzip.decompress(base64.b64decode(payload)))

    def packed_body_chunks(self, source):
        root = html.document_fromstring(source)
        result = {}
        for node in root.xpath("//script[@data-offline-body-chunk]"):
            values = json.loads(gzip.decompress(base64.b64decode(node.text)))
            result.update(dict(values))
        return {int(key): value for key, value in result.items()}

    def test_css_escaped_font_url_matches_the_recorded_encoded_url(self):
        a = new_archive("https://example.com/")
        css = r'@font-face {font-family:Example;src:url("/font\20 name.woff2");}'
        a["entries"] = [
            entry(
                a["url"],
                (
                    "<html><head><style>"
                    + css
                    + '</style><link rel="stylesheet" href="/sheet.css"></head><body>Page</body></html>'
                ).encode(),
                "text/html",
            ),
            entry("https://example.com/sheet.css", css.encode(), "text/css"),
            entry("https://example.com/font%20name.woff2", b"recorded-font", "font/woff2"),
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "page.html"
            result = pack(a, path)
            doc = html.document_fromstring(path.read_text())
        self.assertFalse(result["warnings"], result["warnings"])
        embedded = "data:font/woff2;base64," + base64.b64encode(b"recorded-font").decode()
        self.assertIn(embedded, doc.find(".//style").text)
        sheet = base64.b64decode(doc.find(".//link").get("href").split(",")[1]).decode()
        self.assertIn(embedded, sheet)

    def test_metadata_links_are_not_missing_downloads(self):
        a = new_archive("https://example.com/")
        a["entries"] = [
            entry(
                a["url"],
                b'<html><head><link rel="canonical" href="https://example.com/canonical"><link rel="alternate" hreflang="de" href="https://example.de/"><link rel="alternate stylesheet" href="/theme.css" title="Theme"></head><body>Page</body></html>',
                "text/html",
            ),
            entry("https://example.com/theme.css", b"body{color:blue}", "text/css"),
        ]
        with tempfile.TemporaryDirectory() as d:
            result = pack(a, Path(d) / "page.html")
        self.assertFalse(result["warnings"], result["warnings"])

    def test_partial_response_is_not_embedded_as_a_complete_asset(self):
        a = new_archive("https://example.com/")
        a["entries"] = [
            entry(
                a["url"], b'<html><body><video src="/clip.webm"></video></body></html>', "text/html"
            ),
            entry(
                "https://example.com/clip.webm",
                b"partial",
                mime="video/webm",
                status=206,
                headers={"content-range": "bytes 0-6/100", "content-type": "video/webm"},
            ),
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "snapshot.html"
            result = pack(a, p)
            doc = html.document_fromstring(p.read_text())
        self.assertIn(
            "Uncaptured static resource: https://example.com/clip.webm", result["warnings"]
        )
        self.assertNotIn(base64.b64encode(b"partial").decode(), doc.find(".//video").get("src"))

    def test_query_matching_preserves_repeated_value_order(self):
        self.assertEqual(
            canonical_url("https://example.com?a=1&z=x%20y&a=2#top"),
            "https://example.com/?a=1&a=2&z=x+y",
        )
        self.assertNotEqual(
            canonical_url("https://example.com?a=1&a=2"),
            canonical_url("https://example.com?a=2&a=1"),
        )

    def test_query_keys_match_browser_urlsearchparams(self):
        import subprocess

        urls = [
            "https://example.org/?q=*&z=~&a=two&a=one",
            "https://example.org/?q=%2A&z=%7e&space=x%20y&empty",
            "https://example.org/?%EE%80%80=first&%F0%90%80%80=second&x=%2B%21%28%29%27",
        ]
        script = "const urls=JSON.parse(process.argv[1]);console.log(JSON.stringify(urls.map(value=>{const u=new URL(value);u.searchParams.sort();return u.href})))"
        expected = json.loads(
            subprocess.run(
                ["node", "-e", script, json.dumps(urls)], capture_output=True, text=True, check=True
            ).stdout
        )
        self.assertEqual([canonical_url(url) for url in urls], expected)

    def test_embedded_json_cannot_close_script(self):
        obj = {"value": "</script><script>alert(1)</script>\u2028\u2029"}
        encoded = script_json(obj)
        self.assertNotIn("<", encoded)
        self.assertEqual(json.loads(encoded), obj)

    def test_missing_entry_document_fails(self):
        with self.assertRaisesRegex(ValueError, "entry document"):
            pack(new_archive("https://example.com/"), "unused.html")

    def test_css_nested_assets_and_bootstrap_are_portable(self):
        a = new_archive("https://example.com/page")
        a["entries"] = [
            entry(
                a["url"],
                b'<html><head><base href="/"><link rel="stylesheet" href="css/site.css"><script defer src="app.js"></script></head><body><img src="photo.png"></body></html>',
                "text/html",
            ),
            entry("https://example.com/app.js", b"window.started=true", "application/javascript"),
            entry(
                "https://example.com/css/site.css",
                b'@import "other.css";body{background:url(../photo.png)}',
                "text/css",
            ),
            entry(
                "https://example.com/css/other.css", b"@font-face{src:url(font.woff2)}", "text/css"
            ),
            entry("https://example.com/css/font.woff2", b"font", "font/woff2"),
            entry("https://example.com/photo.png", b"image", "image/png"),
        ]
        original = copy.deepcopy(a)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "snapshot.html"
            result = pack(a, path)
            doc = html.document_fromstring(path.read_text())
        self.assertFalse(result["warnings"])
        self.assertEqual(
            doc.find(".//meta[@http-equiv]").get("http-equiv"), "Content-Security-Policy"
        )
        scripts = doc.findall(".//script")
        self.assertIn("window.__OFFLINE_ARCHIVE__=", scripts[0].text)
        self.assertTrue(scripts[1].get("src").startswith("data:application/javascript;base64,"))
        css = base64.b64decode(doc.find(".//link").get("href").split(",")[1]).decode()
        self.assertIn("data:text/css;base64,", css)
        self.assertIn("data:image/png;base64,", css)
        # Packaging may transform its private copy, never the supplied recording.
        self.assertEqual(a, original)

    def test_post_request_bodies_remain_distinct(self):
        x = entry("https://example.com/api", b"a", method="POST", request_body=b'{"q":1}')
        y = entry("https://example.com/api", b"b", method="POST", request_body=b'{"q":2}')
        self.assertNotEqual(x["requestBody"], y["requestBody"])

    def test_static_fallback_has_no_scripts_events_or_online_links(self):
        a = new_archive("https://example.com/page")
        a["entries"] = [
            entry(a["url"], b"<html><head></head><body>Hello</body></html>", "text/html"),
            entry("https://example.com/photo.png", b"image", "image/png"),
        ]
        a["snapshots"] = [
            {
                "name": "initial",
                "url": a["url"],
                "html": '<html><head><script src="tracker.js"></script></head><body onload="danger()"><a href="https://example.com/next"><img src="/photo.png" onerror="danger()"></a></body></html>',
            }
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "snapshot.html"
            pack(a, path)
            root = html.document_fromstring(path.read_text())
        payload = root.find(".//script").text.split(";\n", 1)[0].split("=", 1)[1]
        replay = json.loads(payload)
        static = html.document_fromstring(replay["staticViews"][0]["html"])
        self.assertEqual(static.findall(".//script"), [])
        self.assertFalse(static.xpath("//*[@onload or @onerror]"))
        self.assertFalse(static.xpath("//a[@href]"))
        self.assertTrue(static.find(".//img").get("src").startswith("data:image/png;"))

    def test_linked_documents_share_archive_and_keep_original_scripts(self):
        a = new_archive("https://example.com/start")
        a["pages"] = [{"url": a["url"]}, {"url": "https://example.com/nested/page"}]
        a["entries"] = [
            entry(
                a["url"],
                b'<html><head></head><body><a href="/nested/page">Next</a></body></html>',
                "text/html",
            ),
            entry(
                a["pages"][1]["url"],
                b'<html><head><script src="app.js"></script></head><body>Linked original document</body></html>',
                "text/html",
            ),
            entry(
                "https://example.com/nested/app.js",
                b"window.originalScriptRan=true",
                "application/javascript",
            ),
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "multi.html"
            pack(a, path)
            source = path.read_text()
        bundle = self.packed_bundle(source)
        self.assertEqual(len(bundle["archive"]["entries"]), 3)
        self.assertEqual(len(bundle["documents"]), 2)
        self.assertEqual(bundle["archive"]["resourceStorage"]["mode"], "shared_resource_table")
        child = html.document_fromstring(
            bundle["documents"]["https://example.com/nested/page"]["html"]
        )
        self.assertEqual(child.find(".//script").text, bundle["marker"])
        import subprocess

        token = child.findall(".//script")[1].get("src")
        self.assertEqual(
            bundle["archive"]["resourceTokens"][token], "https://example.com/nested/app.js"
        )
        record = next(
            item
            for item in bundle["archive"]["entries"]
            if item["url"] == "https://example.com/nested/app.js"
        )
        code = base64.b64decode(bundle["archive"]["responseBodies"][record["bodyRef"]]).decode()
        runner = "const vm=require('node:vm'),fs=require('node:fs');const window={};vm.runInNewContext(fs.readFileSync(0,'utf8'),{window,__offlineWindow:window});process.stdout.write(JSON.stringify(window.originalScriptRan));"
        result = subprocess.run(
            ["node", "-e", runner], input=code, text=True, capture_output=True, check=True
        )
        self.assertEqual(result.stdout, "true")
        self.assertNotIn(
            "__OFFLINE_ARCHIVE__", bundle["documents"]["https://example.com/nested/page"]["html"]
        )

    def test_multipage_binary_assets_are_stored_once(self):
        a = new_archive("https://example.com/start")
        a["pages"] = [{"url": a["url"]}, {"url": "https://example.com/next"}]
        image = bytes(range(256)) * 1024
        a["entries"] = [
            entry(
                a["url"],
                b'<html><body><img src="/shared.bin"><a href="/next">Next</a></body></html>',
                "text/html",
            ),
            entry(
                "https://example.com/next",
                b'<html><body><img src="/shared.bin"></body></html>',
                "text/html",
            ),
            entry("https://example.com/shared.bin", image, "image/png"),
            entry("https://example.com/shared.bin", image, "image/png"),
        ]
        encoded = base64.b64encode(image).decode()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "multi.html"
            second = Path(d) / "multi-again.html"
            result = pack(a, path)
            pack(a, second)
            source = path.read_text()
            self.assertEqual(path.read_bytes(), second.read_bytes())
        bundle = self.packed_bundle(source)
        self.assertEqual(bundle["archive"]["responseBodies"].count(encoded), 1)
        self.assertNotIn(encoded, source)
        self.assertEqual(result["resourceStorage"]["mode"], "shared_resource_table")
        self.assertEqual(result["resourceStorage"]["uniqueResources"], 1)
        self.assertEqual(result["responseStorage"]["mode"], "shared_body_table")
        self.assertEqual(result["responseStorage"]["responseRecords"], 4)
        self.assertEqual(result["responseStorage"]["uniqueBodies"], 3)
        self.assertEqual(result["bundleStorage"]["mode"], "gzip_base64")
        self.assertGreater(result["bundleStorage"]["savedBytes"], 0)
        self.assertLess(result["bytes"], 500000)

    def test_async_responses_are_chunked_without_deferring_sync_resources(self):
        a = new_archive("https://example.com/start")
        a["pages"] = [{"url": a["url"]}, {"url": "https://example.com/next"}]
        records = [
            entry(a["url"], b"<html><body>Start</body></html>", "text/html"),
            entry("https://example.com/next", b"<html><body>Next</body></html>", "text/html"),
            entry(
                "https://example.com/async",
                b'{"value":"' + b"x" * 200000 + b'"}',
                "application/json",
            ),
            entry("https://example.com/fetch", b"fetch response" * 10000, "text/plain"),
            entry("https://example.com/sync", b"synchronous", "text/plain"),
        ]
        records[2].update(resourceType="xhr", xhrMode="async")
        records[3]["resourceType"] = "fetch"
        records[4].update(resourceType="xhr", xhrMode="sync")
        a["entries"] = records
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "chunked.html"
            second = Path(d) / "chunked-again.html"
            result = pack(a, path)
            pack(a, second)
            source = path.read_text()
            self.assertEqual(path.read_bytes(), second.read_bytes())
        bundle = self.packed_bundle(source)
        chunks = self.packed_body_chunks(source)
        by_url = {item["url"]: item for item in bundle["archive"]["entries"]}
        bodies = bundle["archive"]["responseBodies"]
        for url in ("https://example.com/async", "https://example.com/fetch"):
            reference = by_url[url]["bodyRef"]
            self.assertIsNone(bodies[reference])
            original = next(item for item in records if item["url"] == url)
            self.assertEqual(chunks[reference], original["body"])
        sync = by_url["https://example.com/sync"]["bodyRef"]
        self.assertEqual(base64.b64decode(bodies[sync]), b"synchronous")
        self.assertEqual(result["responseStorage"]["deferredBodies"], 2)
        self.assertEqual(result["bundleStorage"]["mode"], "gzip_base64_chunked")

    def test_worker_capture_keeps_async_responses_eager(self):
        a = new_archive("https://example.com/start")
        a["pages"] = [{"url": a["url"]}]
        document = entry(a["url"], b"<html><body>Start</body></html>", "text/html")
        response = entry("https://example.com/api", b"worker response", "text/plain")
        response["resourceType"] = "fetch"
        a["entries"] = [document, response]
        a["workerURLs"] = ["https://example.com/worker.js"]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "worker.html"
            result = pack(a, path)
            source = path.read_text()
        bundle = self.packed_bundle(source)
        record = next(
            item for item in bundle["archive"]["entries"] if item["url"] == response["url"]
        )
        self.assertEqual(
            base64.b64decode(bundle["archive"]["responseBodies"][record["bodyRef"]]),
            b"worker response",
        )
        self.assertEqual(result["responseStorage"]["deferredBodies"], 0)
        self.assertIn("worker", result["responseStorage"]["deferredDisabledReason"].lower())


if __name__ == "__main__":
    unittest.main()

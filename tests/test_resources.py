import unittest

from offline_snapshot.asset_capture import static_mime
from offline_snapshot.capture import (
    PublicStaticResponseCache,
    StaticFailureCircuit,
    annotate_xhr_modes,
)
from offline_snapshot.resource_policy import link_kind


class ResourceTests(unittest.TestCase):
    def test_only_explicitly_public_static_responses_are_reused(self):
        diagnostics = {}
        cache = PublicStaticResponseCache(diagnostics, max_bytes=20)
        base = {"content-type": "application/javascript", "cache-control": "public, max-age=3600"}
        cache.store("GET", "https://cdn.test/app.js", "script", 200, {}, base, b"public")
        cached = cache.lookup("GET", "https://cdn.test/app.js#fragment", "script", {})
        self.assertEqual(cached["body"], b"public")
        cache.fulfilled(cached)
        self.assertIsNone(
            cache.lookup("GET", "https://cdn.test/app.js", "script", {"cache-control": "no-cache"})
        )
        for number, headers in enumerate(
            (
                {"cache-control": "max-age=3600"},
                {"cache-control": "public, no-store, max-age=3600"},
                {"cache-control": "public, max-age=3600", "vary": "accept-language"},
                {"cache-control": "public, immutable", "set-cookie": "session=secret"},
                {"cache-control": "public, max-age=60", "age": "60"},
            )
        ):
            cache.store(
                "GET",
                f"https://cdn.test/rejected-{number}.js",
                "script",
                200,
                {},
                headers,
                b"private",
            )
        cache.store("GET", "https://cdn.test/api", "xhr", 200, {}, base, b"api")
        cache.store("POST", "https://cdn.test/post.js", "script", 200, {}, base, b"post")
        cache.store(
            "GET",
            "https://cdn.test/range.js",
            "script",
            200,
            {"range": "bytes=0-3"},
            base,
            b"range",
        )
        report = diagnostics["publicStaticCache"]
        self.assertEqual(
            (report["storedResources"], report["fulfilledRequests"], report["bytesAvoided"]),
            (1, 1, 6),
        )
        self.assertIsNone(
            cache.lookup("GET", "https://cdn.test/range.js", "script", {"range": "bytes=0-3"})
        )

    def test_failed_static_request_is_suppressed_without_affecting_api_calls(self):
        diagnostics = {}
        circuit = StaticFailureCircuit(diagnostics)
        self.assertFalse(circuit.suppress("GET", "https://cdn.test/app.js", "script"))
        circuit.failed("GET", "https://cdn.test/app.js#first", "script")
        self.assertTrue(circuit.suppress("GET", "https://cdn.test/app.js#second", "script"))
        self.assertTrue(circuit.suppress("GET", "https://cdn.test/app.js", "script"))
        circuit.failed("GET", "https://cdn.test/api", "xhr")
        circuit.failed("POST", "https://cdn.test/app.js", "script")
        self.assertFalse(circuit.suppress("GET", "https://cdn.test/api", "xhr"))
        self.assertFalse(circuit.suppress("POST", "https://cdn.test/app.js", "script"))
        report = diagnostics["staticFailureCircuit"]
        self.assertEqual(report["suppressedRequests"], 2)
        self.assertEqual(
            report["resources"],
            [
                {
                    "method": "GET",
                    "url": "https://cdn.test/app.js",
                    "resourceType": "script",
                    "priorFailures": 1,
                    "suppressedRequests": 2,
                }
            ],
        )

    def test_only_exclusively_async_observed_xhr_is_marked_lazy_safe(self):
        records = [
            {"method": "GET", "url": "https://example.test/api?b=2&a=1", "resourceType": "xhr"},
            {"method": "GET", "url": "https://example.test/sync", "resourceType": "xhr"},
            {"method": "POST", "url": "https://example.test/mixed", "resourceType": "xhr"},
            {"method": "GET", "url": "https://example.test/unknown", "resourceType": "xhr"},
            {"method": "GET", "url": "https://example.test/image", "resourceType": "image"},
        ]
        observations = {
            ("GET", "https://example.test/api?a=1&b=2"): {True},
            ("GET", "https://example.test/sync"): {False},
            ("POST", "https://example.test/mixed"): {True, False},
        }
        counts = annotate_xhr_modes(records, observations)
        self.assertEqual(
            [item.get("xhrMode") for item in records], ["async", "sync", "sync", "unknown", None]
        )
        self.assertEqual(counts, {"async": 1, "sync": 2, "unknown": 1})

    def test_link_token_classification(self):
        for rel in ["canonical", "alternate", "alternate author", "license", ""]:
            self.assertEqual(link_kind(rel), "metadata")
        for rel in ["ALTERNATE stylesheet", "shortcut icon", "apple-touch-icon", "mask-icon"]:
            self.assertEqual(link_kind(rel), "asset")
        for rel in ["preload", "modulepreload", "dns-prefetch", "manifest"]:
            self.assertEqual(link_kind(rel), "hint")

    def test_declared_asset_mime_rejects_api_and_error_documents(self):
        for kind in ["image", "media", "link", "font"]:
            self.assertFalse(static_mime(kind, "text/html; charset=UTF-8"))
            self.assertFalse(static_mime(kind, "application/json"))
        self.assertTrue(static_mime("media", "audio/wav"))
        self.assertTrue(static_mime("media", "video/webm"))
        self.assertTrue(static_mime("image", "image/svg+xml"))
        self.assertFalse(static_mime("media", "application/vnd.apple.mpegurl"))
        self.assertTrue(static_mime("font", "font/woff2"))
        self.assertFalse(static_mime("font", "text/css"))


if __name__ == "__main__":
    unittest.main()

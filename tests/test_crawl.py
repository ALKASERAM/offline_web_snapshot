import unittest

from offline_snapshot.crawl import CrawlPlan, page_url


class CrawlTests(unittest.TestCase):
    def test_depth_zero_does_not_schedule_links(self):
        p = CrawlPlan("https://example.org/", 0)
        p.discover(p.start, 0, [{"href": "/one"}])
        self.assertIsNone(p.next())
        self.assertEqual(p.report["skipped"][0]["reason"], "depth limit")

    def test_breadth_first_two_hops_and_cycles(self):
        p = CrawlPlan("https://example.org/", 2)
        p.discover(p.start, 0, [{"href": "/a"}, {"href": "/b"}, {"href": "/a#section"}])
        a = p.next()
        self.assertEqual(a["url"], "https://example.org/a")
        p.discover(a["url"], 1, [{"href": "/"}, {"href": "/c"}])
        self.assertEqual(p.next()["url"], "https://example.org/b")
        c = p.next()
        self.assertEqual(c["depth"], 2)
        p.discover(c["url"], 2, [{"href": "/too-far"}])
        self.assertIsNone(p.next())
        self.assertEqual(p.report["skipped"][-1]["reason"], "depth limit")

    def test_page_limit_counts_start_and_failed_attempts(self):
        p = CrawlPlan("https://example.org/", 5, 2)
        p.discover(p.start, 0, [{"href": "/fails"}, {"href": "/not-visited"}])
        p.next()["status"] = "failed"
        self.assertIsNone(p.next())
        self.assertTrue(p.report["limitReached"])

    def test_scope_downloads_actions_and_path_filters(self):
        p = CrawlPlan("https://example.org/", 2, 25, ["/records/*"], ["/records/private/*"])
        links = [
            {"href": h}
            for h in [
                "https://elsewhere.org/records/1",
                "mailto:a@example.org",
                "/records/file.pdf",
                "/logout",
                "/records/private/1",
                "/about",
                "/records/12",
            ]
        ]
        links.append({"href": "/records/export", "download": True})
        p.discover(p.start, 0, links)
        self.assertEqual(p.next()["url"], "https://example.org/records/12")
        self.assertIsNone(p.next())
        self.assertEqual(len(p.report["skipped"]), 7)

    def test_query_values_and_hash_routes_are_not_conflated(self):
        self.assertEqual(page_url("https://e.org/p?b=2&a=1#text"), "https://e.org/p?a=1&b=2")
        self.assertNotEqual(page_url("https://e.org/?q=1"), page_url("https://e.org/?q=2"))
        self.assertNotEqual(page_url("https://e.org/#/one"), page_url("https://e.org/#/two"))
        self.assertNotEqual(page_url("https://e.org/?a=1&a=2"), page_url("https://e.org/?a=2&a=1"))

    def test_invalid_limits_fail(self):
        for depth, limit in [(-1, 25), (2, 0)]:
            with self.assertRaises(ValueError):
                CrawlPlan("https://example.org", depth, limit)

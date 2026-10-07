"""Bounded breadth-first discovery of ordinary document links."""

import re
from collections import deque
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit

from .archive import canonical_url


def page_url(url):
    """Ignore document anchors, but keep query parameters and SPA hash routes."""
    parts = urlsplit(url)
    fragment = parts.fragment if parts.fragment.startswith(("!/", "/")) else ""
    return canonical_url(url) + ("#" + fragment if fragment else "")


def origin(url):
    p = urlsplit(url)
    return p.scheme.lower(), p.hostname, p.port or (443 if p.scheme == "https" else 80)


def route(url):
    p = urlsplit(url)
    return p.path + ("?" + p.query if p.query else "")


class CrawlPlan:
    def __init__(self, start, depth=0, max_pages=25, include=(), exclude=()):
        if depth < 0 or max_pages < 1:
            raise ValueError("depth must be nonnegative and max_pages must be at least 1")
        self.start = page_url(start)
        self.depth, self.max_pages = depth, max_pages
        self.include, self.exclude = tuple(include), tuple(exclude)
        self.queue = deque()
        self.seen = {self.start}
        self.report = {
            "depth": depth,
            "maxPages": max_pages,
            "sameOrigin": True,
            "includePaths": list(include),
            "excludePaths": list(exclude),
            "pages": [{"url": self.start, "depth": 0, "status": "recorded"}],
            "skipped": [],
            "limitReached": False,
            "note": "Depth counts link hops from the start. Explicit recipe navigation is additional. Recorded pages are not an offline acceptance pass.",
        }
        self._skipped = set()

    def skip(self, url, parent, reason):
        key = (url, reason)
        if key not in self._skipped:
            self.report["skipped"].append({"url": url, "from": parent, "reason": reason})
            self._skipped.add(key)

    def exclusion(self, url, download=False):
        p = urlsplit(url)
        if p.scheme not in ("http", "https"):
            return "not an HTTP page link"
        if p.username or p.password:
            return "credentials in URL"
        if origin(url) != origin(self.start):
            return "different website origin"
        if download or PurePosixPath(p.path.lower()).suffix in {
            ".pdf",
            ".zip",
            ".gz",
            ".tar",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".svg",
            ".webp",
            ".mp4",
            ".mp3",
            ".woff",
            ".woff2",
            ".css",
            ".js",
            ".xml",
            ".json",
            ".nxz",
            ".obj",
            ".glb",
        }:
            return "download or non-HTML resource"
        if re.search(
            r"/(?:login|logout|signin|signout|signup|register|delete|remove|checkout)(?:/|$)",
            p.path,
            re.I,
        ):
            return "account or action link"
        if self.include and not any(fnmatchcase(p.path, pattern) for pattern in self.include):
            return "outside included paths"
        if any(fnmatchcase(p.path, pattern) for pattern in self.exclude):
            return "excluded path"
        return None

    def discover(self, parent, depth, links):
        for link in links:
            raw = link.get("href", "").strip()
            if not raw or (raw.startswith("#") and not raw.startswith(("#/", "#!/"))):
                continue
            try:
                url = page_url(urljoin(parent, raw))
                reason = self.exclusion(url, link.get("download", False))
            except ValueError:
                self.skip(raw, parent, "invalid URL")
                continue
            if reason:
                self.skip(url, parent, reason)
            elif url in self.seen:
                continue
            elif depth >= self.depth:
                self.skip(url, parent, "depth limit")
            elif len(self.seen) >= self.max_pages:
                self.report["limitReached"] = True
                self.skip(url, parent, "page limit")
            else:
                self.seen.add(url)
                self.queue.append(
                    {"url": url, "from": parent, "depth": depth + 1, "status": "queued"}
                )

    def next(self):
        if not self.queue:
            return None
        item = self.queue.popleft()
        self.report["pages"].append(item)
        return item

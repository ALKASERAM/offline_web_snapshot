"""Portable capture data. Bodies are decoded HTTP bodies, encoded as base64."""

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_url(url):
    parts = urlsplit(url)
    # Match URLSearchParams.sort()/serialization in the browser. Python's
    # default quoting escapes '*' but leaves '~'; browsers do the reverse.
    # UTF-16 key ordering and stable sorting retain repeated parameter order.
    query = urlencode(
        sorted(
            parse_qsl(parts.query, keep_blank_values=True),
            key=lambda p: p[0].encode("utf-16-be", "surrogatepass"),
        ),
        safe="*",
    ).replace("~", "%7E")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", query, ""))


def new_archive(url, method="browser"):
    return {
        "format": "offline-snapshot/1",
        "url": url,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "collectionMethod": method,
        "entries": [],
        "snapshots": [],
        "routes": [],
        "warnings": [],
    }


def entry(
    url,
    body,
    mime="application/octet-stream",
    status=200,
    method="GET",
    request_body=b"",
    headers=None,
):
    return {
        "url": url,
        "method": method.upper(),
        "requestBody": base64.b64encode(request_body).decode(),
        "status": status,
        "mime": mime,
        "headers": headers or {"content-type": mime},
        "body": base64.b64encode(body).decode(),
    }


def decode(record):
    return base64.b64decode(record["body"], validate=True)


def load(path):
    data = json.loads(Path(path).read_text())
    if data.get("format") != "offline-snapshot/1":
        raise ValueError("Unsupported archive format")
    if urlsplit(data["url"]).scheme not in ("http", "https"):
        raise ValueError("Capture source must use HTTP or HTTPS")
    for item in data["entries"]:
        decode(item)
        if not 100 <= item["status"] <= 599:
            raise ValueError("Invalid HTTP response status")
    return data


def save(data, path):
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )


SENSITIVE_KEYS = {
    "accesstoken",
    "apikey",
    "auth",
    "authorization",
    "csrftoken",
    "email",
    "idtoken",
    "jwt",
    "password",
    "passwd",
    "phone",
    "refreshtoken",
    "secret",
    "session",
    "sessionid",
    "token",
}


def _sensitive(name):
    key = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return key in SENSITIVE_KEYS or key.endswith(("apikey", "password", "secret", "token"))


def _encoded_size(value):
    value = value or ""
    return max(0, len(value) * 3 // 4 - value[-2:].count("="))


def _body_keys(raw):
    """Return field names only; never place captured values in an audit."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return set()
    keys = set()
    try:
        value = json.loads(text)

        def walk(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    keys.add(str(key))
                    walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)

        walk(value)
    except (ValueError, TypeError):
        try:
            keys.update(key for key, _ in parse_qsl(text, keep_blank_values=True))
        except ValueError:
            pass
    return keys


def privacy_audit(data):
    """Summarize potential sensitivity without returning captured values."""
    query_counts = {}
    body_count = body_bytes = api_count = api_bytes = 0
    sensitive_body_keys = set()
    sensitive_response_headers = set()
    origins = set()
    for item in data.get("entries", []):
        parts = urlsplit(item.get("url", ""))
        if parts.scheme in ("http", "https") and parts.netloc:
            origins.add(parts.scheme + "://" + parts.netloc)
        for key, _ in parse_qsl(parts.query, keep_blank_values=True):
            if _sensitive(key):
                query_counts[key] = query_counts.get(key, 0) + 1
        request_body = item.get("requestBody", "")
        size = _encoded_size(request_body)
        if size:
            body_count += 1
            body_bytes += size
            try:
                sensitive_body_keys.update(
                    key
                    for key in _body_keys(base64.b64decode(request_body, validate=True))
                    if _sensitive(key)
                )
            except (ValueError, base64.binascii.Error):
                pass
        mime = item.get("mime", "").lower()
        sensitive_response_headers.update(
            name for name in (item.get("headers") or {}) if _sensitive(name)
        )
        if item.get("resourceType") in ("fetch", "xhr") or "json" in mime:
            api_count += 1
            api_bytes += _encoded_size(item.get("body", ""))
    cookies = sorted(
        {
            cookie.get("name", "")
            for page in data.get("pages", [])
            for cookie in page.get("cookieSeed", [])
            if cookie.get("name")
        }
    )
    storage = data.get("storage") or {}
    local_keys = sorted((storage.get("localStorage") or {}).keys())
    session_keys = sorted((storage.get("sessionStorage") or {}).keys())
    sensitive_queries = [
        {"name": key, "occurrences": query_counts[key]} for key in sorted(query_counts)
    ]
    potential = bool(
        sensitive_queries or body_count or cookies or local_keys or session_keys or api_count
    )
    recommendations = []
    if cookies:
        recommendations.append(
            "Review script-visible cookie seeds or capture again without --capture-cookies."
        )
    if sensitive_queries:
        recommendations.append(
            "Review sensitive query parameter names and exclude private endpoints where possible."
        )
    if body_count:
        recommendations.append(
            "Request bodies are required for exact replay; review POST/API scope before sharing."
        )
    if api_count:
        recommendations.append(
            "Captured API response bodies may contain personal or account data; inspect the source scope before sharing."
        )
    return {
        "status": "attention_required" if potential else "no_obvious_sensitive_state",
        "containsPotentiallySensitiveData": potential,
        "heuristic": True,
        "note": "This value-free audit cannot prove an archive is safe to share; response bodies and page content require owner review.",
        "origins": sorted(origins),
        "sensitiveQueryParameters": sensitive_queries,
        "requestBodies": {
            "count": body_count,
            "bytes": body_bytes,
            "sensitiveFieldNames": sorted(sensitive_body_keys),
        },
        "apiResponses": {"count": api_count, "bytes": api_bytes},
        "sensitiveResponseHeaderNames": sorted(sensitive_response_headers),
        "cookieSeeds": {
            "count": sum(len(page.get("cookieSeed", [])) for page in data.get("pages", [])),
            "names": cookies,
        },
        "storageSeeds": {"localStorageKeys": local_keys, "sessionStorageKeys": session_keys},
        "recommendations": recommendations,
    }

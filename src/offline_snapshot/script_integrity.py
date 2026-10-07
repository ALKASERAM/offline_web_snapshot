"""Check external scripts; bounded HTTP recovery happens only during capture."""

import asyncio
import base64
import hashlib
from urllib.parse import urljoin, urlsplit

from .archive import decode
from .javascript import check_script_sources
from .resource_policy import excluded


def script_record(record):
    return (
        record["method"] == "GET"
        and record["status"] == 200
        and not record.get("requestBody")
        and not record.get("requestHeaders", {}).get("range")
        and any(kind in record["mime"].lower() for kind in ("javascript", "ecmascript"))
    )


async def complete_scripts(archive, request_context, *, timeout, exclusions, total, monitor=None):
    """Never guess bytes or repair a changed version. Keep source damage visible.

    Syntax validity is not a general proof that a response is complete. It does
    detect the unterminated strings produced by interrupted compressed scripts.
    Original body bytes are retained on any recovered record for inspection.
    """
    groups = {}
    for index, record in enumerate(archive["entries"]):
        if script_record(record):
            key = (
                record["url"],
                record["body"],
                record["headers"].get("etag"),
                record["headers"].get("last-modified"),
            )
            groups.setdefault(key, []).append(index)
    keys = list(groups)
    errors = await asyncio.to_thread(
        check_script_sources, [base64.b64decode(k[1]).decode("utf-8", "replace") for k in keys]
    )
    report = {
        "checkedSources": len(keys),
        "recoveries": [],
        "failures": [],
        "note": "Syntax check only, not a complete-response proof. Recovered source state may need recapture.",
    }
    archive["scriptIntegrity"] = report
    for key, error in zip(keys, errors, strict=False):
        if not error:
            continue
        indices = groups[key]
        record = archive["entries"][indices[0]]
        original = decode(record)
        evidence = {
            "url": record["url"],
            "entryIndices": indices,
            "originalBytes": len(original),
            "originalSHA256": hashlib.sha256(original).hexdigest(),
            "parserError": error[:1000],
        }
        response = None
        operation = ("script", record["url"])
        try:
            headers = record["headers"]
            if not (headers.get("etag") or headers.get("last-modified")):
                raise ValueError(
                    "No recorded version validator; cannot safely complete this script"
                )
            retry_headers = {}
            if headers.get("etag") and not headers["etag"].startswith("W/"):
                retry_headers["if-match"] = headers["etag"]
            elif headers.get("last-modified"):
                retry_headers["if-unmodified-since"] = headers["last-modified"]
            target = record["url"]
            deadline = asyncio.get_running_loop().time() + timeout / 1000
            if monitor:
                monitor.start_request(operation, target, "GET", "script", "script completion")
                monitor.emit("script_retry")
            for hop in range(6):
                if urlsplit(target).scheme not in ("http", "https") or excluded(target, exclusions):
                    raise ValueError("Script completion destination is excluded or is not HTTP(S)")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("Script redirect chain exceeded request timeout")
                response = await request_context.get(
                    target, headers=retry_headers, timeout=max(1, remaining * 1000), max_redirects=0
                )
                if response.status not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get("location")
                if not location or hop == 5:
                    raise ValueError("Invalid or excessive script redirects")
                target = urljoin(target, location)
                await response.dispose()
                response = None
            if response.status != 200:
                raise ValueError("Script completion returned HTTP " + str(response.status))
            received = dict(response.headers)
            if not any(
                kind in received.get("content-type", "").lower()
                for kind in ("javascript", "ecmascript")
            ):
                raise ValueError("Script completion did not return JavaScript")
            for name in ("etag", "last-modified"):
                if headers.get(name) and received.get(name) != headers[name]:
                    raise ValueError("Script version changed: " + name)
            body = await response.body()
            if len(body) <= len(original) or not body.startswith(original):
                raise ValueError(
                    "Response is not a longer byte-for-byte completion of the captured prefix"
                )
            delta = (len(body) - len(original)) * len(indices)
            if len(body) > 20 * 1024 * 1024 or total + delta > 150 * 1024 * 1024:
                raise ValueError("Script completion exceeds resource budget")
            if (await asyncio.to_thread(check_script_sources, [body.decode("utf-8", "replace")]))[
                0
            ]:
                raise ValueError("Recollected JavaScript is still invalid")
            evidence.update(
                method="capture-time HTTP completion of malformed script",
                bytes=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                finalUrl=response.url,
            )
            for index in indices:
                target_record = archive["entries"][index]
                target_record["captureRecovery"] = {
                    **evidence,
                    "originalBody": target_record["body"],
                }
                target_record["body"] = base64.b64encode(body).decode()
                target_record["collectionMethod"] = evidence["method"]
            total += delta
            report["recoveries"].append(evidence)
            archive["warnings"].append(
                "Malformed script recollected; source page state needs verification: "
                + record["url"]
            )
            if "HTTP completion of malformed scripts" not in archive["collectionMethod"]:
                archive["collectionMethod"] += " + HTTP completion of malformed scripts"
        except Exception as exc:
            evidence["error"] = str(exc)[:1000]
            report["failures"].append(evidence)
            archive["warnings"].append(
                "Invalid recorded JavaScript could not be completed: "
                + record["url"]
                + ": "
                + str(exc)[:200]
            )
        finally:
            if response is not None:
                await response.dispose()
            if monitor:
                monitor.end_request(operation)
    return total

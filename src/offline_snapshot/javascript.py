"""Prepare archived JavaScript with a parser, without executing it or fetching."""

import base64
import json
import re
import subprocess  # nosec B404
from pathlib import Path
from urllib.parse import urljoin

from lxml import etree, html

from .archive import canonical_url, decode
from .toolchain import parser_environment, required_executable


def check_script_sources(sources):
    """Parse source strings without running them or applying replay transforms."""
    jobs = [{"id": str(i), "code": code, "checkOnly": True} for i, code in enumerate(sources)]
    if not jobs:
        return []
    # The executable and packaged script are fixed; captured source travels only through stdin.
    result = subprocess.run(  # nosec B603
        [required_executable("node"), str(Path(__file__).with_name("rewrite.cjs"))],
        input=json.dumps(jobs),
        text=True,
        capture_output=True,
        timeout=120,
        env=parser_environment(),
    )
    if result.returncode:
        raise RuntimeError("JavaScript integrity parser failed: " + result.stderr[:1000])
    return [item.get("error") for item in json.loads(result.stdout)]


def prepare_scripts(archive):
    jobs = []
    targets = {}
    documents = []
    archive["documentSettings"] = {}
    document_urls = {canonical_url(archive["url"])}
    document_urls.update(
        canonical_url(p.get("documentUrl", p["url"])) for p in archive.get("pages", [])
    )
    document_urls.update(canonical_url(url) for url in archive.get("frameURLs", []))
    for rec in archive["entries"]:
        if rec["method"] != "GET" or not 200 <= rec["status"] < 300:
            continue
        if "javascript" in rec["mime"] or "ecmascript" in rec["mime"]:
            key = str(len(jobs))
            targets[key] = rec
            jobs.append(
                {"id": key, "url": rec["url"], "code": decode(rec).decode("utf-8", "replace")}
            )
        elif "text/html" in rec["mime"]:
            # HTML returned to fetch/XHR can be a fragment or API value. Adding
            # document wrappers would corrupt the recorded response contract.
            if (
                canonical_url(rec["url"]) not in document_urls
                and rec.get("resourceType") != "document"
            ):
                continue
            # Empty tracking / beacon responses sometimes advertise text/html.
            # They are valid recorded responses, not documents to parse.
            if not decode(rec).strip():
                continue
            charset = re.search(r'charset\s*=\s*["\']?([^;\s"\']+)', rec["mime"], re.I)
            doc = html.document_fromstring(
                decode(rec),
                parser=html.HTMLParser(huge_tree=True, encoding=charset[1] if charset else None),
            )
            base_el = doc.find(".//base")
            base = (
                urljoin(rec["url"], base_el.get("href", "")) if base_el is not None else rec["url"]
            )
            # libxml invents/repairs doctypes (even <!DOCTYPE> becomes HTML5),
            # which changes browser layout mode. Preserve the original token.
            prolog = re.match(
                r"\s*(?:(?:<!--[\s\S]*?-->|<\?xml[^>]*\?>)\s*)*(<!doctype\b[^>]*>)?",
                decode(rec).decode("utf-8", "replace").lstrip("\ufeff"),
                re.I,
            )
            doctype = prolog[1] or "" if prolog else ""
            settings = {
                "base": base,
                "documentBase": base if base_el is not None else None,
                "baseAttribute": base_el.get("href") if base_el is not None else None,
                "doctype": doctype,
                "importMap": {"imports": {}, "scopes": {}},
            }
            for node in doc.iter("script"):
                kind = node.get("type", "").lower()
                if kind == "importmap":
                    try:
                        mapping = json.loads(node.text or "{}")
                        settings["importMap"]["imports"].update(mapping.get("imports", {}))
                        settings["importMap"]["scopes"].update(mapping.get("scopes", {}))
                    except ValueError:
                        archive["warnings"].append("Invalid import map: " + rec["url"])
                    node.getparent().remove(node)
                elif (
                    not node.get("src")
                    and kind in ("", "module", "text/javascript", "application/javascript")
                    and node.text
                ):
                    key = str(len(jobs))
                    targets[key] = node
                    jobs.append({"id": key, "url": rec["url"], "base": base, "code": node.text})
            archive["documentSettings"][canonical_url(rec["url"])] = settings
            documents.append((rec, doc))
    if jobs:
        try:
            # The executable and packaged script are fixed; no shell is involved.
            result = subprocess.run(  # nosec B603
                [required_executable("node"), str(Path(__file__).with_name("rewrite.cjs"))],
                input=json.dumps(jobs),
                text=True,
                capture_output=True,
                timeout=120,
                env=parser_environment(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                "JavaScript packaging requires Node.js 18+ and offline-snapshot setup"
            ) from exc
        if result.returncode:
            raise RuntimeError(
                "JavaScript parser failed; run offline-snapshot doctor: " + result.stderr[:1000]
            )
        for item in json.loads(result.stdout):
            if item.get("error"):
                raise ValueError(
                    "Cannot safely rewrite captured JavaScript "
                    + jobs[int(item["id"])]["url"]
                    + ": "
                    + item["error"][:1000]
                )
            target = targets[item["id"]]
            code = item["code"]
            if isinstance(target, dict):
                # Native stacks otherwise repeat the entire data URL at every
                # frame. Source libraries that inspect Error.stack can stall on
                # megabytes of encoded source. This names the transformed script
                # without fetching the URL or changing execution order.
                source_url = re.sub(
                    r"[\s\u2028\u2029]", lambda m: "%" + format(ord(m[0]), "02X"), target["url"]
                )
                code += "\n//# sourceURL=" + source_url + "\n"
                target["body"] = base64.b64encode(code.encode()).decode()
            else:
                target.text = code.replace("</script", "<\\/script")
            archive["warnings"].extend(
                "JavaScript transform: " + w for w in item.get("warnings", [])
            )
    for rec, doc in documents:
        # The transformed document is serialized as UTF-8. Make that explicit
        # for frames that originally relied on an HTTP Content-Type charset.
        head = doc.find("head")
        if head is None:
            head = etree.Element("head")
            doc.insert(0, head)
        for meta in list(head.iter("meta")):
            if meta.get("charset") or meta.get("http-equiv", "").lower() == "content-type":
                meta.getparent().remove(meta)
        head.insert(0, etree.Element("meta", charset="utf-8"))
        rec["body"] = base64.b64encode(html.tostring(doc, encoding="utf-8")).decode()
        rec["mime"] = "text/html;charset=utf-8"
        rec.setdefault("headers", {})["content-type"] = rec["mime"]
    archive["scriptTransforms"] = {
        "engine": "esbuild + es-module-lexer",
        "scripts": len(jobs),
        "changes": [
            "Virtual source location and base URL",
            "Recorded module graph resolved with embedded import maps",
            "Original source names in transformed script stacks",
        ],
        "limits": [
            "Dynamically evaluated source strings are not rewritten",
            "Reflective access to native browser descriptors may bypass virtual location",
        ],
    }

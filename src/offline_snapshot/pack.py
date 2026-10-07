"""Compile a capture into a standalone HTML file; never fetch during packaging."""

import base64
import gzip
import json
import re
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from lxml import etree, html

from .archive import canonical_url, decode, privacy_audit
from .crawl import page_url
from .javascript import prepare_scripts
from .resource_policy import excluded, link_kind

CSS_URL = re.compile(r'url\(\s*([\'"]?)(.*?)\1\s*\)', re.I)
CSS_IMPORT = re.compile(r'@import\s+(?:url\(\s*)?[\'"]([^\'"]+)[\'"]\s*\)?([^;]*);', re.I)
BODY_CHUNK_TARGET_BYTES = 2 * 1024 * 1024
BODY_CHUNK_CACHE_BYTES = 8 * 1024 * 1024


def css_reference(value):
    """Decode a CSS URL token's escapes before resolving its HTTP resource key."""

    def unescape(match):
        token = match[1]
        if token[0] in "\r\n\f":
            return ""
        if re.match("[0-9a-fA-F]", token):
            number = int(token.strip(), 16)
            return (
                chr(number)
                if 0 < number <= 0x10FFFF and not 0xD800 <= number <= 0xDFFF
                else "\ufffd"
            )
        return token

    decoded = re.sub(r"\\([0-9a-fA-F]{1,6}(?:\r\n|[\t\n\f\r ])?|\r\n|[\s\S])", unescape, value)
    return quote(decoded, safe=":/?#[]@!$&'()*+,;=%~")


def script_json(value):
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def data_url(body, mime):
    return "data:" + mime.split(";")[0] + ";base64," + base64.b64encode(body).decode()


def working_archive(archive):
    """Copy only structures mutated by packing; large immutable bodies stay shared."""
    result = dict(archive)
    result["entries"] = [
        {**record, "headers": dict(record.get("headers") or {})}
        for record in archive.get("entries", [])
    ]
    result["warnings"] = list(archive.get("warnings", []))
    result["pages"] = [dict(page) for page in archive.get("pages", [])]
    result["snapshots"] = [dict(snapshot) for snapshot in archive.get("snapshots", [])]
    if "pageAliases" in archive:
        result["pageAliases"] = dict(archive["pageAliases"])
    if "renderedAssets" in archive:
        result["renderedAssets"] = {
            url: [dict(item) for item in items] for url, items in archive["renderedAssets"].items()
        }
    return result


def compact_response_bodies(archive):
    """Store equal response bodies once while retaining every response record."""
    bodies = []
    indexes = {}
    original_bytes = 0
    for record in archive["entries"]:
        body = record.pop("body", "")
        original_bytes += len(body)
        index = indexes.get(body)
        if index is None:
            index = len(bodies)
            indexes[body] = index
            bodies.append(body)
        record["bodyRef"] = index
    archive["responseBodies"] = bodies
    storage = {
        "mode": "shared_body_table",
        "responseRecords": len(archive["entries"]),
        "uniqueBodies": len(bodies),
        "encodedBytesBefore": original_bytes,
        "encodedBytesStored": sum(map(len, bodies)),
    }
    archive["responseStorage"] = storage
    return storage


def compressed_bundle(bundle):
    """Return a deterministic, browser-decodable bundle and storage evidence."""
    raw = script_json(bundle).encode()
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    encoded = base64.b64encode(compressed).decode()
    return encoded, {
        "mode": "gzip_base64",
        "uncompressedBytes": len(raw),
        "compressedBytes": len(compressed),
        "encodedBytes": len(encoded),
        "savedBytes": len(raw) - len(encoded),
    }


def deferred_response_chunks(archive, resource_tokens):
    """Move only provably asynchronous response bodies into gzip chunks."""
    bodies = archive["responseBodies"]
    usages = {index: [] for index in range(len(bodies))}
    for record in archive["entries"]:
        usages[record["bodyRef"]].append(record)
    resources = {}
    for record in archive["entries"]:
        if record["method"] == "GET" and 200 <= record["status"] < 300 and record["status"] != 206:
            resources[canonical_url(record["url"])] = record
    protected = set()
    for url in resource_tokens.values():
        record = resources.get(canonical_url(url))
        if record is not None:
            protected.add(record["bodyRef"])
    lazy = []
    if not archive.get("workerURLs"):
        for reference, records in usages.items():
            asynchronous = all(
                record.get("resourceType") == "fetch"
                or (record.get("resourceType") == "xhr" and record.get("xhrMode") == "async")
                for record in records
            )
            if asynchronous and reference not in protected and bodies[reference]:
                lazy.append(reference)
    groups = []
    current = []
    current_bytes = 0
    for reference in lazy:
        size = len(bodies[reference])
        if current and current_bytes + size > BODY_CHUNK_TARGET_BYTES:
            groups.append(current)
            current = []
            current_bytes = 0
        current.append(reference)
        current_bytes += size
    if current:
        groups.append(current)
    references = [None] * len(bodies)
    payloads = []
    metadata = []
    for index, group in enumerate(groups):
        values = [[reference, bodies[reference]] for reference in group]
        payload, storage = compressed_bundle(values)
        encoded_body_bytes = sum(len(bodies[reference]) for reference in group)
        payloads.append(payload)
        metadata.append(
            {
                **storage,
                "index": index,
                "bodies": len(group),
                "encodedBodyBytes": encoded_body_bytes,
            }
        )
        for reference in group:
            references[reference] = index
            bodies[reference] = None
    archive["responseBodyChunks"] = {
        "version": 1,
        "refs": references,
        "chunks": metadata,
        "cacheLimitBytes": BODY_CHUNK_CACHE_BYTES,
    }
    storage = archive["responseStorage"]
    storage.update(
        {
            "deferredMode": "gzip_chunks",
            "eagerBodies": len(bodies) - len(lazy),
            "deferredBodies": len(lazy),
            "eagerEncodedBytes": sum(len(body) for body in bodies if body is not None),
            "deferredEncodedBytes": sum(item["encodedBodyBytes"] for item in metadata),
            "chunks": len(payloads),
        }
    )
    if archive.get("workerURLs"):
        storage["deferredDisabledReason"] = (
            "Classic worker responses require synchronous body availability."
        )
    return payloads, metadata


def chunked_bundle_storage(core, chunks):
    if not chunks:
        return core
    totals = {
        name: core[name] + sum(chunk[name] for chunk in chunks)
        for name in ("uncompressedBytes", "compressedBytes", "encodedBytes")
    }
    return {
        "mode": "gzip_base64_chunked",
        **totals,
        "savedBytes": totals["uncompressedBytes"] - totals["encodedBytes"],
        "core": core,
        "bodyChunks": {
            "count": len(chunks),
            **{
                name: sum(chunk[name] for chunk in chunks)
                for name in ("uncompressedBytes", "compressedBytes", "encodedBytes")
            },
        },
    }


def static_snapshot(snapshot, archive, resource):
    """A script-free saved layout, included as a fallback beside replay."""
    doc = html.document_fromstring(snapshot["html"])
    base_el = doc.find(".//base")
    base = (
        urljoin(snapshot["url"], base_el.get("href", ""))
        if base_el is not None
        else snapshot["url"]
    )
    for node in list(doc.iter()):
        if node is not doc and doc not in node.iterancestors():
            continue
        if not isinstance(node.tag, str):
            continue
        tag = node.tag.lower()
        if tag in ("script", "base", "noscript") or (tag == "meta" and node.get("http-equiv")):
            if node.getparent() is not None:
                node.getparent().remove(node)
            continue
        if node.getparent() is None and node is not doc:
            continue
        if tag == "iframe" or node.get("id") == "entityMap":
            node.clear()
            node.tag = "div"
            node.set("class", "well")
            node.text = "Embedded viewer not available in this snapshot."
        for k in list(node.attrib):
            if k.lower().startswith(("on", "ng-", "data-ng-")) or k.lower() in (
                "srcset",
                "integrity",
                "crossorigin",
                "nonce",
                "ping",
            ):
                del node.attrib[k]
        if tag == "link" and node.get("rel") not in ("stylesheet", "icon", "shortcut icon"):
            node.getparent().remove(node)
            continue
        if tag == "a":
            node.attrib.pop("href", None)
            node.attrib.pop("target", None)
        for attr in ("src", "poster"):
            if not node.get(attr):
                continue
            node.set(attr, resource(node.get(attr), base))
        if tag == "link" and node.get("href"):
            node.set("href", resource(node.get("href"), base))
        if node.get("style"):
            node.set(
                "style",
                CSS_URL.sub(
                    lambda m: 'url("' + resource(css_reference(m[2]), base) + '")',
                    node.get("style"),
                ),
            )
        if tag == "style" and node.text:
            node.text = CSS_URL.sub(
                lambda m: 'url("' + resource(css_reference(m[2]), base) + '")', node.text
            )
    head = doc.find("head")
    if head is None:
        head = etree.Element("head")
        doc.insert(0, head)
    head.insert(
        0,
        etree.Element(
            "meta",
            {
                "http-equiv": "Content-Security-Policy",
                "content": "default-src 'none'; img-src data:; style-src 'unsafe-inline' data:; font-src data:; media-src data:; form-action 'none'",
            },
        ),
    )
    banner = etree.Element(
        "div",
        style="position:relative;z-index:2147483647;margin-top:55px;padding:12px;background:#fff3cd;color:#352a00;font:14px system-ui",
    )
    banner.text = (
        "Captured static view: "
        + snapshot["name"]
        + ". Original page scripts and links are disabled."
    )
    body = doc.find("body")
    if body is None:
        body = etree.Element("body")
        doc.append(body)
    body.insert(0, banner)
    for node in doc.iter("a"):
        node.attrib.pop("data-original-href", None)
    return "<!doctype html>\n" + html.tostring(doc, encoding="unicode")


def pack(archive, destination):
    a = working_archive(archive)
    a["privacyAudit"] = privacy_audit(a)
    # Re-key old recordings as well as new captures without mutating their
    # source files. The browser indexes page URLs using URLSearchParams.
    if "pages" in a:
        a["pages"] = [{**page, "url": page_url(page["url"])} for page in a["pages"]]
    if "pageAliases" in a:
        a["pageAliases"] = {
            page_url(key): page_url(value) for key, value in a["pageAliases"].items()
        }
    if "renderedAssets" in a:
        a["renderedAssets"] = {page_url(key): value for key, value in a["renderedAssets"].items()}
    index = {
        canonical_url(e["url"]): e
        for e in a["entries"]
        if e["method"] == "GET" and 200 <= e["status"] < 300 and e["status"] != 206
    }
    source = index.get(canonical_url(a["url"]))
    if not source:
        raise ValueError("Archive is missing the original entry document")
    doc = html.document_fromstring(decode(source))
    base_el = doc.find(".//base")
    base = urljoin(a["url"], base_el.get("href", "")) if base_el is not None else a["url"]
    a["base"] = base
    a["warnings"] = list(a.get("warnings", []))
    memo = {}
    shared_resources = bool(a.get("pages"))
    resource_tokens = {}
    token_by_url = {}
    resource_references = 0

    def shared_token(url):
        token = token_by_url.get(url)
        if token is None:
            token = "offline-snapshot-resource:" + str(len(resource_tokens))
            resource_tokens[token] = url
            token_by_url[url] = token
        return token

    def resource(ref, parent, stack=()):
        nonlocal resource_references
        if not ref or ref.startswith(("data:", "blob:", "#")):
            return ref
        url = canonical_url(urljoin(parent, ref))
        if excluded(url, a.get("resourceExclusions", [])):
            a["warnings"].append("Explicit resource exclusion: " + url)
            return (
                "data:application/javascript," if urlsplit(url).path.endswith(".js") else "data:,"
            )
        if url in memo:
            resource_references += 1
            return memo[url]
        rec = index.get(url)
        if not rec:
            a["warnings"].append("Uncaptured static resource: " + url)
            return "data:application/octet-stream;base64,"
        if url in stack:
            a["warnings"].append("Cyclic CSS import: " + url)
            return "data:text/css;base64,"
        body = decode(rec)
        if "text/css" in rec["mime"]:
            text = body.decode("utf-8", "replace")
            text = CSS_IMPORT.sub(
                lambda m: (
                    '@import url("'
                    + resource(css_reference(m[1]), url, (*stack, url))
                    + '")'
                    + m[2]
                    + ";"
                ),
                text,
            )
            text = CSS_URL.sub(
                lambda m: 'url("' + resource(css_reference(m[2]), url, (*stack, url)) + '")', text
            )
            body = text.encode()
            rec["body"] = base64.b64encode(body).decode()
        memo[url] = shared_token(url) if shared_resources else data_url(body, rec["mime"])
        resource_references += 1
        return memo[url]

    # Compile CSS dependencies, including dynamically requested CSS chunks.
    for rec in a["entries"]:
        if "text/css" in rec["mime"]:
            resource(rec["url"], base)

    prepare_scripts(a)
    doc = html.document_fromstring(decode(source), parser=html.HTMLParser(huge_tree=True))
    a.update(a.get("documentSettings", {}).get(canonical_url(a["url"]), {}))
    runtime_code = (
        Path(__file__)
        .with_name("runtime.js")
        .read_text()
        .replace(
            "// __OFFLINE_CSS_RUNTIME__",
            Path(__file__).with_name("css_urls.js").read_text()
            + "\n"
            + Path(__file__).with_name("css_runtime.js").read_text(),
        )
        + "\n"
        + Path(__file__).with_name("browser_state.js").read_text()
    )
    if a.get("scriptTransforms"):
        a["workerRuntime"] = Path(__file__).with_name("worker_runtime.js").read_text()

    a["staticViews"] = []
    for snapshot in a.get("snapshots", []):
        # Preserve gallery image identifiers while stripping navigation links.
        rendered = html.document_fromstring(snapshot["html"])
        for link in rendered.iter("a"):
            if link.get("href"):
                link.set("data-original-href", link.get("href"))
        saved = {**snapshot, "html": html.tostring(rendered, encoding="unicode")}
        a["staticViews"].append(
            {"name": snapshot["name"], "html": static_snapshot(saved, a, resource)}
        )
    a["snapshots"] = [{k: v for k, v in s.items() if k != "html"} for s in a.get("snapshots", [])]

    def compile_document(doc, base, bootstrap, document_url=None, stack=()):
        document_url = document_url or a["url"]
        candidates = {}
        for item in a.get("renderedAssets", {}).get(document_url, []):
            if item.get("src"):
                candidates.setdefault(item["src"], set()).add(item["currentSrc"])
        selected = {
            src: next(iter(values)) for src, values in candidates.items() if len(values) == 1
        }
        if any(len(values) > 1 for values in candidates.values()):
            a["warnings"].append(
                "Ambiguous rendered image selection retained original src: " + document_url
            )
        for node in list(doc.iter()):
            if node is not doc and doc not in node.iterancestors():
                continue
            tag = node.tag.lower() if isinstance(node.tag, str) else ""
            if node.getparent() is None and node is not doc:
                continue
            if tag == "meta" and node.get("http-equiv", "").lower() in (
                "content-security-policy",
                "refresh",
            ):
                node.getparent().remove(node)
                continue
            if tag == "link" and node.get("href"):
                ref = node.get("href")
                kind = link_kind(node.get("rel"))
                if a.get("scriptTransforms"):
                    node.set("data-offline-link-href", urljoin(base, ref))
                    node.set("data-offline-link-kind", kind)
                if kind == "hint":
                    node.attrib.pop("href", None)
                elif kind == "metadata":
                    # Keep discoverable metadata without treating its destination
                    # as a downloadable asset. Runtime getters expose the source.
                    node.set("href", "data:text/css,")
                else:
                    node.set("href", resource(ref, base))
            if tag == "base":
                # Keep the element: some applications query it. Relative URLs are virtualised.
                # An href on an opaque file origin violates base-uri 'self' in Firefox.
                node.attrib.pop("href", None)
                continue
            if tag == "link":
                # Image preloads can fetch through imagesrcset without any href.
                node.attrib.pop("imagesrcset", None)
                node.attrib.pop("imagesizes", None)
            for attr in ("integrity", "crossorigin", "nonce", "ping"):
                node.attrib.pop(attr, None)
            if a.get("scriptTransforms") and "autofocus" in node.attrib:
                node.attrib.pop("autofocus")
                node.set("data-offline-autofocus", "")
            if tag == "iframe":
                ref = node.attrib.pop("src", None)
                frame_url = canonical_url(urljoin(base, ref)) if ref else None
                rec = index.get(frame_url)
                if rec and "text/html" in rec["mime"] and frame_url not in stack and len(stack) < 8:
                    settings = a.get("documentSettings", {}).get(frame_url, {"base": frame_url})
                    overrides = {
                        **settings,
                        "url": frame_url,
                        "embeddedNavigation": False,
                        "subframe": True,
                        "initialHash": None,
                    }
                    setup = (
                        "window.__OFFLINE_ARCHIVE__=Object.assign({},parent.__OFFLINE_ARCHIVE__,"
                        + script_json(overrides)
                        + ");\n"
                        + runtime_code
                    )
                    frame_doc = html.document_fromstring(
                        decode(rec), parser=html.HTMLParser(huge_tree=True)
                    )
                    a["frameDocuments"][frame_url] = compile_document(
                        frame_doc, settings["base"], setup, frame_url, (*stack, frame_url)
                    )
                    node.set("data-offline-frame-url", frame_url)
                elif ref in (None, "", "about:blank"):
                    pass  # An intentional empty frame needs no recorded document.
                elif not node.get("srcdoc"):
                    node.set("srcdoc", "<p>Embedded viewer not available in this snapshot.</p>")
                    a["warnings"].append("Embedded frame unavailable: " + str(frame_url))
            if tag == "img" and node.get("src") in selected:
                chosen = selected[node.get("src")]
                if not chosen.startswith("blob:"):
                    node.set("src", chosen)
            if (
                tag == "script"
                and node.get("type") == "module"
                and node.get("src")
                and a.get("scriptTransforms")
            ):
                module_url = canonical_url(urljoin(base, node.attrib.pop("src")))
                node.text = "import " + script_json(module_url) + ";"
            if tag == "script" and node.get("src") and a.get("scriptTransforms"):
                node.set("data-offline-source", urljoin(base, node.get("src")))
            for attr in ("src", "poster"):
                if attr in node.attrib:
                    node.set(attr, resource(node.get(attr), base))
            if node.get("style"):
                node.set(
                    "style",
                    CSS_URL.sub(
                        lambda m: 'url("' + resource(css_reference(m[2]), base) + '")',
                        node.get("style"),
                    ),
                )
            if tag == "style" and node.text:
                node.text = CSS_URL.sub(
                    lambda m: 'url("' + resource(css_reference(m[2]), base) + '")', node.text
                )
            if node.get("srcset"):
                # Initial image.src is retained. Responsive source sets require a dedicated parser.
                node.attrib.pop("srcset", None)
                a["warnings"].append("Responsive image fixed to the captured viewport selection")
            if tag == "script" and node.get("type") == "module" and not a.get("scriptTransforms"):
                a["warnings"].append("ES module graph rewriting is not implemented")
        head = doc.find("head")
        if head is None:
            head = etree.Element("head")
            doc.insert(0, head)
        policy = etree.Element(
            "meta",
            {
                "http-equiv": "Content-Security-Policy",
                "content": "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' data: blob:; style-src 'unsafe-inline' data: blob:; img-src data: blob:; font-src data: blob:; media-src data: blob:; connect-src data: blob:; frame-src blob: about:; worker-src blob:; object-src 'none'; form-action 'none'; base-uri 'self'",
            },
        )
        # CSP and replay setup precede every original script.
        runtime = etree.Element("script")
        runtime.text = bootstrap() if callable(bootstrap) else bootstrap
        head.insert(0, runtime)
        head.insert(0, policy)
        head.insert(0, etree.Element("meta", charset="utf-8"))
        doctype = (
            a.get("documentSettings", {})
            .get(canonical_url(document_url), {})
            .get("doctype", "<!doctype html>")
        )
        out = doctype + "\n" + html.tostring(doc, encoding="unicode")
        return out

    pages = a.get("pages", [])
    a["frameDocuments"] = {}
    if a.get("scriptTransforms"):
        for frame_url in a.get("frameURLs", []):
            frame_url = canonical_url(frame_url)
            rec = index.get(frame_url)
            if not rec or "text/html" not in rec["mime"] or not decode(rec).strip():
                continue
            settings = a.get("documentSettings", {}).get(frame_url, {"base": frame_url})
            overrides = {
                **settings,
                "url": frame_url,
                "embeddedNavigation": False,
                "subframe": True,
                "initialHash": None,
            }
            setup = (
                "window.__OFFLINE_ARCHIVE__=Object.assign({},parent.__OFFLINE_ARCHIVE__,"
                + script_json(overrides)
                + ");\n"
                + runtime_code
            )
            frame_doc = html.document_fromstring(
                decode(rec), parser=html.HTMLParser(huge_tree=True)
            )
            a["frameDocuments"][frame_url] = compile_document(
                frame_doc, settings["base"], setup, frame_url, (frame_url,)
            )
    bundle_storage = {"mode": "direct_script"}
    if pages:
        # Store the response archive once. Each embedded document gets a fresh
        # replay runtime when selected, so its original scripts execute normally.
        documents = {}
        marker = "/* OFFLINE_PAGE_BOOTSTRAP */"
        for page in pages:
            rec = index.get(canonical_url(page.get("documentUrl", page["url"])))
            if rec is None:
                a["warnings"].append("Missing linked document: " + page["url"])
                continue
            page_doc = html.document_fromstring(decode(rec))
            page_base_el = page_doc.find(".//base")
            page_base = (
                urljoin(page["url"], page_base_el.get("href", ""))
                if page_base_el is not None
                else page["url"]
            )
            documents[page["url"]] = {
                "url": page["url"],
                "base": page_base,
                "settings": {
                    **a.get("documentSettings", {}).get(canonical_url(rec["url"]), {}),
                    "cookieSeed": page.get("cookieSeed", []),
                },
                "html": compile_document(page_doc, page_base, marker, page["url"]),
            }
        a["pageURLs"] = list(documents)
        a["resourceTokens"] = resource_tokens
        a["resourceStorage"] = {
            "mode": "shared_resource_table",
            "uniqueResources": len(resource_tokens),
            "compiledReferences": resource_references,
            "note": "Static resource bytes are stored once in the response archive; document tokens become local data/blob URLs before parsing.",
        }
        compact_response_bodies(a)
        body_payloads, body_metadata = deferred_response_chunks(a, resource_tokens)
        bundle = {"archive": a, "documents": documents, "runtime": runtime_code, "marker": marker}
        payload, core_storage = compressed_bundle(bundle)
        bundle_storage = chunked_bundle_storage(core_storage, body_metadata)
        shell = html.document_fromstring(
            '<html><head></head><body><div id="offline-snapshot-loading" role="status">Opening offline snapshot…</div><iframe id="offline-page" title="Captured page"></iframe></body></html>'
        )
        shell_head = shell.find("head")
        shell_head.append(etree.Element("meta", charset="utf-8"))
        shell_head.append(
            etree.Element(
                "meta",
                {
                    "http-equiv": "Content-Security-Policy",
                    "content": "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' data: blob:; style-src 'unsafe-inline' data: blob:; img-src data: blob:; font-src data: blob:; media-src data: blob:; frame-src blob:; worker-src blob:; connect-src data: blob:; base-uri 'none'; form-action 'none'",
                },
            )
        )
        style = etree.SubElement(shell_head, "style")
        style.text = "html,body{margin:0;height:100%;overflow:hidden}#offline-page{border:0;width:100%;height:100%;display:block}#offline-snapshot-loading{position:fixed;inset:0;display:grid;place-items:center;background:#fff;color:#234;font:16px system-ui;z-index:1}"
        data = etree.SubElement(
            shell_head,
            "script",
            {"id": "offline-snapshot-bundle", "type": "application/octet-stream"},
        )
        data.text = payload
        script = etree.SubElement(shell_head, "script")
        script.text = (
            Path(__file__).with_name("bundle_loader.js").read_text()
            + "\n"
            + Path(__file__).with_name("multipage.js").read_text()
        )
        for index, body_payload in enumerate(body_payloads):
            chunk = etree.SubElement(
                shell_head,
                "script",
                {
                    "id": "offline-snapshot-body-" + str(index),
                    "type": "application/octet-stream",
                    "data-offline-body-chunk": str(index),
                },
            )
            chunk.text = body_payload
        out = b"<!doctype html>\n" + html.tostring(shell, encoding="utf-8")
    else:

        def archive_bootstrap():
            # Resource compilation above still reads response records. Compact
            # only when the runtime is injected at the end of document work.
            compact_response_bodies(a)
            return "window.__OFFLINE_ARCHIVE__=" + script_json(a) + ";\n" + runtime_code

        out = compile_document(doc, base, archive_bootstrap).encode()
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    Path(destination).write_bytes(out)
    return {
        "output": str(destination),
        "bytes": len(out),
        "responses": len(a["entries"]),
        "warnings": sorted(set(a["warnings"])),
        "resourceStorage": a.get("resourceStorage", {"mode": "inline_data_urls"}),
        "responseStorage": a["responseStorage"],
        "bundleStorage": bundle_storage,
        "privacyAudit": a["privacyAudit"],
    }

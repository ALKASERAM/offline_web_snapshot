"""Conservative discovery of non-form browser interactions.

Discovery is deliberately narrower than link crawling or an interaction recipe.
It identifies controls that may reveal additional client-side state while leaving
forms, account actions, mutations, downloads and ordinary navigation links alone.
"""

import re
from hashlib import sha256
from urllib.parse import urljoin, urlsplit

_MUTATION_WORDS = re.compile(
    r"\b(?:accept|agree|apply|approve|book|buy|checkout|confirm|create|delete|"
    r"destroy|erase|favorite|follow|join|like|login|log[ -]?out|order|pay|"
    r"publish|purchase|register|remove|report|reserve|save|send|sign[ -]?in|"
    r"sign[ -]?out|signup|submit|subscribe|unfollow|unlike|unsubscribe|upload|"
    r"vote)\b",
    re.IGNORECASE,
)


# This script only describes controls. It never sends a click or changes the DOM.
_CANDIDATE_SCRIPT = r"""
options => {
  const include = options.include || [];
  const exclude = options.exclude || [];
  const invalid = [];
  for (const selector of [...include, ...exclude]) {
    try { document.querySelector(selector); }
    catch (error) { invalid.push({selector, error: String(error)}); }
  }
  if (invalid.length) return {invalid, candidates: []};

  const visible = element => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) > 0 && style.pointerEvents !== 'none' &&
      rect.width > 0 && rect.height > 0;
  };
  const attrSelector = (name, value) =>
    '[' + name + '="' + String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"]';
  const unique = selector => {
    try { return document.querySelectorAll(selector).length === 1; }
    catch (_) { return false; }
  };
  const selectorFor = element => {
    if (element.id) {
      const value = '#' + CSS.escape(element.id);
      if (unique(value)) return value;
    }
    for (const name of ['data-testid', 'data-test', 'data-cy']) {
      const value = element.getAttribute(name);
      if (!value) continue;
      const selector = element.localName + attrSelector(name, value);
      if (unique(selector)) return selector;
    }
    const aria = element.getAttribute('aria-label');
    if (aria) {
      const selector = element.localName + attrSelector('aria-label', aria);
      if (unique(selector)) return selector;
    }
    const path = [];
    for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
      let part = node.localName;
      if (node.id) {
        part += '#' + CSS.escape(node.id);
        path.unshift(part);
        break;
      }
      if (node.parentElement) {
        const siblings = [...node.parentElement.children].filter(item => item.localName === node.localName);
        if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
      }
      path.unshift(part);
      if (node.localName === 'body') break;
    }
    return path.join(' > ');
  };
  const matchesAny = (element, selectors) => selectors.some(selector =>
    element.matches(selector) || Boolean(element.closest(selector)));
  const nodes = [...new Set(document.querySelectorAll(
    'button, summary, input[type="button"], [role="button"], [role="tab"], ' +
    'a[href^="#"], a[href^="javascript:"]'
  ))];
  return {invalid: [], candidates: nodes.map(element => {
    const label = (element.getAttribute('aria-label') || element.innerText ||
      element.getAttribute('title') || element.value || '').replace(/\s+/g, ' ').trim();
    return {
      selector: selectorFor(element),
      tag: element.localName,
      role: (element.getAttribute('role') || '').toLowerCase(),
      type: (element.getAttribute('type') || '').toLowerCase(),
      label: label.slice(0, 160),
      href: element.href || '',
      download: element.hasAttribute('download'),
      disabled: Boolean(element.disabled) || element.getAttribute('aria-disabled') === 'true',
      visible: visible(element),
      inForm: Boolean(element.closest('form')) || Boolean(element.getAttribute('form')),
      contentEditable: element.isContentEditable,
      included: !include.length || matchesAny(element, include),
      excluded: matchesAny(element, exclude),
      policyText: [label, element.id, element.className, element.getAttribute('name') || '']
        .filter(value => typeof value === 'string').join(' ').replace(/[_-]+/g, ' ').slice(0, 500)
    };
  })};
}
"""


def _origin(url):
    parts = urlsplit(url)
    return (
        parts.scheme.lower(),
        parts.hostname,
        parts.port or (443 if parts.scheme == "https" else 80),
    )


def classify_candidate(candidate, page_url):
    """Return a skip reason, or ``None`` when a candidate is eligible."""
    if not candidate.get("visible"):
        return "not visible"
    if candidate.get("disabled"):
        return "disabled"
    if candidate.get("inForm"):
        return "form control"
    if candidate.get("contentEditable"):
        return "editable control"
    if candidate.get("excluded"):
        return "excluded by selector"
    if not candidate.get("included", True):
        return "outside included selectors"
    if candidate.get("download"):
        return "download control"
    if candidate.get("type") in ("submit", "reset", "file"):
        return "form control"
    if not candidate.get("label"):
        return "unnamed control"
    if _MUTATION_WORDS.search(candidate.get("policyText", candidate["label"])):
        return "potential account or data mutation"
    href = candidate.get("href") or ""
    if href:
        target = urljoin(page_url, href)
        parts = urlsplit(target)
        if parts.scheme not in ("http", "https"):
            if not href.lower().startswith("javascript:"):
                return "non-HTTP navigation"
        elif _origin(target) != _origin(page_url):
            return "cross-origin navigation"
        elif candidate.get("tag") == "a" and candidate.get("role") not in ("button", "tab"):
            source = urlsplit(page_url)
            if (parts.scheme, parts.netloc, parts.path, parts.query) != (
                source.scheme,
                source.netloc,
                source.path,
                source.query,
            ):
                return "ordinary navigation link"
    return None


async def scan_candidates(page, include=(), exclude=()):
    """Return eligible and skipped controls without interacting with the page."""
    result = await page.evaluate(
        _CANDIDATE_SCRIPT,
        {
            "include": list(include),
            "exclude": list(exclude),
        },
    )
    if result["invalid"]:
        item = result["invalid"][0]
        raise ValueError(
            f"Invalid interaction discovery selector {item['selector']!r}: {item['error']}"
        )
    eligible = []
    skipped = []
    for candidate in result["candidates"]:
        reason = classify_candidate(candidate, page.url)
        public = {key: candidate[key] for key in ("selector", "tag", "role", "label")}
        if reason:
            skipped.append({**public, "status": "skipped", "reason": reason})
        else:
            eligible.append(public)
    return eligible, skipped


async def state_fingerprint(page):
    """Hash the current URL and markup to detect a material recorded state."""
    markup = await page.content()
    return sha256((page.url + "\0" + markup).encode("utf-8")).hexdigest()


def new_visible_text(before, after, *, limit=160):
    """Return a short newly visible line suitable for a replay assertion."""
    old = " ".join(before.split())
    for line in after.splitlines():
        value = " ".join(line.split())
        if 2 < len(value) <= limit and value not in old:
            return value
    return None

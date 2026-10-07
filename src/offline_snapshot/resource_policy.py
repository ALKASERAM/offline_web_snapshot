"""Explicit user-selected resource omissions; '*' and '?' are URL wildcards."""

import re


def link_kind(rel):
    tokens = set((rel or "").lower().split())
    if tokens & {
        "stylesheet",
        "icon",
        "apple-touch-icon",
        "apple-touch-icon-precomposed",
        "mask-icon",
    }:
        return "asset"
    if tokens & {"preconnect", "dns-prefetch", "prefetch", "preload", "modulepreload", "manifest"}:
        return "hint"
    return "metadata"


def excluded(url, patterns):
    return next(
        (
            pattern
            for pattern in patterns
            if re.fullmatch(re.escape(pattern).replace(r"\*", ".*").replace(r"\?", "."), url)
        ),
        None,
    )

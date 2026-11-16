"""Turn sitemap entries into the ordered list of URLs to warm."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from sitemap_cache_warmer.sitemap import SitemapEntry

ORDERS = ("sitemap", "priority", "lastmod", "priority-lastmod", "url")


def normalize_url(url: str) -> str:
    """Canonical form for de-duplication: lower-case scheme and host, no default port, no fragment."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port and not ((scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)):
        host = f"{host}:{parts.port}"
    return urlunsplit((scheme, host, parts.path or "/", parts.query, ""))


@dataclass
class Selection:
    urls: list[SitemapEntry] = field(default_factory=list)
    duplicates: int = 0
    excluded: int = 0
    too_old: int = 0
    over_limit: int = 0
    invalid: int = 0


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def select(
    entries: list[SitemapEntry],
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    order: str = "sitemap",
    limit: int = 0,
    since: datetime | None = None,
) -> Selection:
    """De-duplicate, filter with regular expressions, sort and limit sitemap entries.

    ``include``/``exclude`` are regular expressions searched in the full URL. ``order`` is one of :data:`ORDERS`;
    ``priority`` and ``lastmod`` sort descending (missing values last) and keep the sitemap order for ties.
    """
    if order not in ORDERS:
        raise ValueError(f"order must be one of {', '.join(ORDERS)}")
    inc = [re.compile(p) for p in include or []]
    exc = [re.compile(p) for p in exclude or []]
    result = Selection()
    seen: set[str] = set()
    kept: list[tuple[int, SitemapEntry]] = []
    for index, entry in enumerate(entries):
        if not entry.loc.startswith(("http://", "https://")):
            result.invalid += 1
            continue
        key = normalize_url(entry.loc)
        if key in seen:
            result.duplicates += 1
            continue
        seen.add(key)
        if (inc and not any(p.search(entry.loc) for p in inc)) or any(p.search(entry.loc) for p in exc):
            result.excluded += 1
            continue
        if since is not None and (entry.lastmod is None or entry.lastmod < since):
            result.too_old += 1
            continue
        kept.append((index, entry))

    def priority(item: tuple[int, SitemapEntry]) -> float:
        return -(item[1].priority if item[1].priority is not None else -1.0)

    def lastmod(item: tuple[int, SitemapEntry]) -> float:
        return -(item[1].lastmod or _EPOCH).timestamp()

    if order == "priority":
        kept.sort(key=lambda it: (priority(it), it[0]))
    elif order == "lastmod":
        kept.sort(key=lambda it: (lastmod(it), it[0]))
    elif order == "priority-lastmod":
        kept.sort(key=lambda it: (priority(it), lastmod(it), it[0]))
    elif order == "url":
        kept.sort(key=lambda it: it[1].loc)
    if limit and len(kept) > limit:
        result.over_limit = len(kept) - limit
        kept = kept[:limit]
    result.urls = [entry for _, entry in kept]
    return result

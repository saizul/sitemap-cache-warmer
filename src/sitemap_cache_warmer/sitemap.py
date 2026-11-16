"""Sitemap reading: ``<urlset>`` and ``<sitemapindex>`` files, plain or gzip compressed, local or remote."""

from __future__ import annotations

import logging
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

import httpx

log = logging.getLogger(__name__)

# The sitemap protocol limits a file to 50 MB uncompressed and 50,000 URLs.
MAX_SITEMAP_BYTES = 50 * 1024 * 1024


class SitemapError(ValueError):
    """The document is not a sitemap we can read."""


@dataclass(frozen=True)
class SitemapEntry:
    loc: str
    lastmod: datetime | None = None
    priority: float | None = None
    changefreq: str | None = None
    sitemap: str = ""
    position: int = 0


@dataclass
class ParsedSitemap:
    kind: str  # "urlset" or "sitemapindex"
    entries: list[SitemapEntry]


def parse_lastmod(value: str | None) -> datetime | None:
    """Parse a W3C datetime (``2026-11-16``, ``2026-11-16T10:00Z``, ``2026-11-16T10:00:00+06:00``...) as UTC."""
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    for candidate in (text, text[:10]):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    if len(text) == 7:  # YYYY-MM
        try:
            return datetime.strptime(text, "%Y-%m").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if len(text) == 4 and text.isdigit():
        return datetime(int(text), 1, 1, tzinfo=timezone.utc)
    return None


def maybe_decompress(content: bytes, *, limit: int = MAX_SITEMAP_BYTES) -> bytes:
    """Gunzip ``content`` when it starts with the gzip magic bytes; refuse output larger than ``limit``."""
    if content[:2] != b"\x1f\x8b":
        return content
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = decoder.decompress(content, limit + 1)
    except zlib.error as exc:
        raise SitemapError(f"broken gzip data: {exc}") from exc
    if len(out) > limit or decoder.unconsumed_tail:
        raise SitemapError(f"compressed sitemap expands to more than {limit} bytes")
    if not decoder.eof:
        raise SitemapError("broken gzip data: truncated stream")
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(content: bytes, *, source: str = "") -> ParsedSitemap:
    """Parse a sitemap document. Namespaces are ignored, so slightly broken sitemaps still work."""
    data = maybe_decompress(content)
    lowered = data.lower()
    if b"<!doctype" in lowered[:4096] or b"<!entity" in lowered:
        # Entity declarations are never needed in a sitemap; refusing them avoids entity expansion attacks.
        raise SitemapError("sitemap contains a DOCTYPE or entity declaration")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise SitemapError(f"not valid XML: {exc}") from exc
    kind = _local(root.tag)
    if kind not in ("urlset", "sitemapindex"):
        raise SitemapError(f"unexpected root element <{kind}>")
    child_tag = "url" if kind == "urlset" else "sitemap"
    entries: list[SitemapEntry] = []
    for node in root:
        if _local(node.tag) != child_tag:
            continue
        fields: dict[str, str] = {}
        for sub in node:
            name = _local(sub.tag)
            if name in ("loc", "lastmod", "priority", "changefreq") and sub.text:
                fields[name] = sub.text.strip()
        loc = fields.get("loc")
        if not loc:
            continue
        priority: float | None = None
        if "priority" in fields:
            try:
                priority = min(1.0, max(0.0, float(fields["priority"])))
            except ValueError:
                priority = None
        entries.append(
            SitemapEntry(
                loc,
                parse_lastmod(fields.get("lastmod")),
                priority,
                fields.get("changefreq"),
                sitemap=source,
                position=len(entries),
            )
        )
    return ParsedSitemap(kind, entries)


@dataclass
class SitemapResult:
    entries: list[SitemapEntry] = field(default_factory=list)
    sitemaps_read: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


async def _load(client: httpx.AsyncClient, source: str) -> bytes:
    if not source.startswith(("http://", "https://")):
        path = Path(source)
        if path.stat().st_size > MAX_SITEMAP_BYTES:
            raise SitemapError("file larger than 50 MB")
        return path.read_bytes()
    async with client.stream("GET", source, follow_redirects=True) as response:
        if response.status_code >= 400:
            raise SitemapError(f"HTTP {response.status_code}")
        chunks = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_SITEMAP_BYTES:
                raise SitemapError("download larger than 50 MB")
            chunks.append(chunk)
        return b"".join(chunks)


async def read_sitemaps(
    client: httpx.AsyncClient,
    sources: list[str],
    *,
    max_depth: int = 3,
    max_sitemaps: int = 1000,
) -> SitemapResult:
    """Read sitemaps (URLs or local files) recursively, following ``<sitemapindex>`` files up to ``max_depth``."""
    result = SitemapResult()
    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(s, 0) for s in sources]
    while queue:
        source, depth = queue.pop(0)
        if source in seen:
            continue
        if len(seen) >= max_sitemaps:
            result.errors.append((source, f"skipped: more than {max_sitemaps} sitemaps"))
            continue
        seen.add(source)
        try:
            content = await _load(client, source)
            parsed = parse_sitemap(content, source=source)
        except (SitemapError, OSError, httpx.HTTPError) as exc:
            message = str(exc) or type(exc).__name__
            log.warning("sitemap %s: %s", source, message)
            result.errors.append((source, message))
            continue
        result.sitemaps_read.append(source)
        if parsed.kind == "sitemapindex":
            if depth >= max_depth:
                result.errors.append((source, f"sitemap index nested deeper than {max_depth} levels"))
                continue
            queue.extend((e.loc, depth + 1) for e in parsed.entries)
        else:
            result.entries.extend(parsed.entries)
    return result

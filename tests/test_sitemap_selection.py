from __future__ import annotations

import asyncio
import gzip
from datetime import datetime, timezone

import httpx
import pytest

from fakecache import HOST, FakeCache, urlset
from sitemap_cache_warmer.selection import normalize_url, select
from sitemap_cache_warmer.sitemap import (
    SitemapEntry,
    SitemapError,
    maybe_decompress,
    parse_lastmod,
    parse_sitemap,
    read_sitemaps,
)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-16", utc(2026, 11, 16)),
        ("2026-11-16T10:30:00Z", utc(2026, 11, 16, 10, 30)),
        ("2026-11-16T10:30:00+06:00", utc(2026, 11, 16, 4, 30)),
        ("2026-11-16T10:30:00.123+00:00", datetime(2026, 11, 16, 10, 30, 0, 123000, tzinfo=timezone.utc)),
        ("2026-11", utc(2026, 11, 1)),
        ("2026", utc(2026, 1, 1)),
        ("2026-11-16 junk", utc(2026, 11, 16)),
        ("yesterday", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_lastmod(text, expected):
    assert parse_lastmod(text) == expected


class TestParse:
    def test_urlset(self):
        parsed = parse_sitemap(urlset([("/a/", "2026-11-01", "0.8"), ("/b/", None, "x")]).encode(), source="s.xml")
        assert parsed.kind == "urlset"
        a, b = parsed.entries
        assert (a.loc, a.lastmod, a.priority, a.sitemap, a.position) == (
            f"{HOST}/a/",
            utc(2026, 11, 1),
            0.8,
            "s.xml",
            0,
        )
        assert b.priority is None and b.lastmod is None and b.position == 1

    def test_gzip(self):
        assert parse_sitemap(gzip.compress(urlset([("/a/", None, None)]).encode())).entries[0].loc == f"{HOST}/a/"

    def test_broken_gzip(self):
        with pytest.raises(SitemapError, match="gzip"):
            maybe_decompress(b"\x1f\x8b\x08\x00garbage")

    def test_gzip_bomb(self):
        with pytest.raises(SitemapError, match="expands"):
            maybe_decompress(gzip.compress(b"0" * 5000), limit=100)

    def test_entities_rejected(self):
        with pytest.raises(SitemapError):
            parse_sitemap(b'<!DOCTYPE urlset [<!ENTITY x "y">]><urlset></urlset>')

    def test_not_a_sitemap(self):
        with pytest.raises(SitemapError):
            parse_sitemap(b"<rss></rss>")


class TestRead:
    def run(self, cache: FakeCache, sources: list[str], **kw):
        async def go():
            async with httpx.AsyncClient(transport=cache.transport) as client:
                return await read_sitemaps(client, sources, **kw)

        return asyncio.run(go())

    def test_index_with_gzip_child(self, cache: FakeCache):
        result = self.run(cache, [f"{HOST}/sitemap_index.xml"])
        assert len(result.sitemaps_read) == 3
        assert len(result.entries) == 7  # the duplicate is removed later, by select()
        assert result.errors == []

    def test_local_file(self, tmp_path, cache: FakeCache):
        path = tmp_path / "sitemap.xml.gz"
        path.write_bytes(gzip.compress(urlset([("/a/", None, None)]).encode()))
        result = self.run(cache, [str(path)])
        assert [e.loc for e in result.entries] == [f"{HOST}/a/"]
        assert cache.requests == []

    def test_errors(self, cache: FakeCache, tmp_path):
        result = self.run(cache, [f"{HOST}/nope.xml", str(tmp_path / "missing.xml")])
        reasons = dict(result.errors)
        assert reasons[f"{HOST}/nope.xml"] == "HTTP 404"
        assert str(tmp_path / "missing.xml") in reasons

    def test_depth_limit(self, cache: FakeCache):
        result = self.run(cache, [f"{HOST}/sitemap_index.xml"], max_depth=0)
        assert result.entries == []
        assert "nested deeper" in result.errors[0][1]


def entries() -> list[SitemapEntry]:
    return [
        SitemapEntry("https://x.io/a", utc(2026, 1, 1), 0.5),
        SitemapEntry("https://x.io/b", utc(2026, 11, 1), 0.9),
        SitemapEntry("https://x.io/c", None, None),
        SitemapEntry("https://X.io:443/a#frag", utc(2026, 1, 1), 0.5),  # duplicate of /a
        SitemapEntry("https://x.io/d", utc(2026, 6, 1), 0.9),
        SitemapEntry("/relative", None, None),
    ]


class TestSelect:
    def test_dedupe_and_sitemap_order(self):
        sel = select(entries())
        assert [e.loc for e in sel.urls] == ["https://x.io/a", "https://x.io/b", "https://x.io/c", "https://x.io/d"]
        assert sel.duplicates == 1
        assert sel.invalid == 1

    def test_priority_order_is_stable(self):
        assert [e.loc[-1] for e in select(entries(), order="priority").urls] == ["b", "d", "a", "c"]

    def test_lastmod_order(self):
        assert [e.loc[-1] for e in select(entries(), order="lastmod").urls] == ["b", "d", "a", "c"]

    def test_priority_then_lastmod(self):
        data = [
            SitemapEntry("https://x.io/old", utc(2025, 1, 1), 0.9),
            SitemapEntry("https://x.io/new", utc(2026, 1, 1), 0.9),
            SitemapEntry("https://x.io/low", utc(2026, 6, 1), 0.1),
        ]
        assert [e.loc[-3:] for e in select(data, order="priority-lastmod").urls] == ["new", "old", "low"]

    def test_url_order(self):
        assert [e.loc[-1] for e in select(entries(), order="url").urls] == ["a", "b", "c", "d"]

    def test_include_exclude(self):
        sel = select(entries(), include=[r"/[abc]$"], exclude=[r"/b$"])
        assert [e.loc[-1] for e in sel.urls] == ["a", "c"]
        assert sel.excluded == 2

    def test_limit(self):
        sel = select(entries(), order="priority", limit=2)
        assert [e.loc[-1] for e in sel.urls] == ["b", "d"]
        assert sel.over_limit == 2

    def test_since(self):
        sel = select(entries(), since=utc(2026, 5, 1))
        assert [e.loc[-1] for e in sel.urls] == ["b", "d"]
        assert sel.too_old == 2  # "a" is too old, "c" has no lastmod

    def test_bad_order(self):
        with pytest.raises(ValueError):
            select(entries(), order="random")


def test_normalize_url():
    assert normalize_url("HTTPS://Shop.Example.com:443/a?b=1#x") == "https://shop.example.com/a?b=1"
    assert normalize_url("http://x.io:8080") == "http://x.io:8080/"

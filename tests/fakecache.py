"""A fake site with a page cache in front of it, served through ``httpx.MockTransport``."""

from __future__ import annotations

import gzip
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

HOST = "https://shop.example.com"


def urlset(entries: list[tuple[str, str | None, str | None]]) -> str:
    """``[(path, lastmod, priority), ...]`` -> urlset XML."""
    items = []
    for path, lastmod, priority in entries:
        extra = (f"<lastmod>{lastmod}</lastmod>" if lastmod else "") + (
            f"<priority>{priority}</priority>" if priority else ""
        )
        items.append(f"<url><loc>{HOST}{path}</loc>{extra}</url>")
    return '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + (
        "".join(items) + "</urlset>"
    )


PAGE_ENTRIES = [
    ("/", "2026-11-10", "1.0"),
    ("/about/", "2026-01-05", "0.5"),
    ("/contact/", None, None),
    ("/cart/", "2026-11-12", "0.1"),
]
POST_ENTRIES = [
    ("/blog/hello/", "2026-11-15T08:00:00+00:00", "0.8"),
    ("/blog/old/", "2024-03-01", "0.3"),
    ("/", "2026-11-10", "1.0"),  # duplicate across sitemaps
]

INDEX = (
    '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    f"<sitemap><loc>{HOST}/page-sitemap.xml</loc></sitemap>"
    f"<sitemap><loc>{HOST}/post-sitemap.xml.gz</loc></sitemap>"
    "</sitemapindex>"
)


def html(title: str, footer: str = "") -> str:
    return f"<!doctype html><html><head><title>{title}</title></head><body><p>{title}</p></body></html>{footer}"


@dataclass
class FakeCache:
    """Pages behind a cache that stores one copy per (path, device, encoding) variant.

    ``style`` picks the headers the cache sends: ``nginx`` (x-fastcgi-cache), ``cloudflare`` (cf-cache-status),
    ``varnish`` (x-varnish + age), ``litespeed`` or ``none``.
    """

    style: str = "nginx"
    bypass_paths: set[str] = field(default_factory=lambda: {"/cart/"})
    missing_paths: set[str] = field(default_factory=lambda: {"/contact/"})  # listed in the sitemap, but 404
    stored: set[tuple[str, str, str]] = field(default_factory=set)
    requests: list[httpx.Request] = field(default_factory=list)
    overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = field(default_factory=dict)
    footer: Callable[[str, bool], str] | None = None

    def variant(self, request: httpx.Request) -> tuple[str, str, str]:
        ua = request.headers.get("user-agent", "")
        device = "mobile" if "Mobile" in ua else "desktop"
        encoding = "br" if "br" in request.headers.get("accept-encoding", "") else "plain"
        return request.url.path, device, encoding

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.overrides:
            return self.overrides[path](request)
        if path == "/sitemap_index.xml":
            return httpx.Response(200, text=INDEX, headers={"content-type": "application/xml"})
        if path == "/page-sitemap.xml":
            return httpx.Response(200, text=urlset(PAGE_ENTRIES), headers={"content-type": "application/xml"})
        if path == "/post-sitemap.xml.gz":
            return httpx.Response(200, content=gzip.compress(urlset(POST_ENTRIES).encode()))
        known = {p for p, _, _ in PAGE_ENTRIES + POST_ENTRIES}
        if path not in known or path in self.missing_paths:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        key = self.variant(request)
        if path in self.bypass_paths:
            status = "BYPASS"
        elif key in self.stored:
            status = "HIT"
        else:
            status = "MISS"
            self.stored.add(key)
        headers = {"content-type": "text/html; charset=UTF-8", "vary": "Accept-Encoding"}
        if self.style == "nginx":
            headers["x-fastcgi-cache"] = status
        elif self.style == "cloudflare":
            headers["cf-cache-status"] = "DYNAMIC" if status == "BYPASS" else status
        elif self.style == "varnish":
            headers["x-varnish"] = "32770 32768" if status == "HIT" else "32770"
            headers["age"] = "12" if status == "HIT" else "0"
            headers["via"] = "1.1 varnish (Varnish/7.5)"
        elif self.style == "litespeed":
            headers["x-litespeed-cache"] = status.lower() if status != "BYPASS" else ""
            if status == "BYPASS":
                headers.pop("x-litespeed-cache")
                headers["x-litespeed-cache-control"] = "no-cache"
        footer = self.footer(path, status == "HIT") if self.footer else ""
        return httpx.Response(200, text=html(path, footer), headers=headers)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

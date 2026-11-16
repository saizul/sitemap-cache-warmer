"""Warm page caches from sitemaps and check that the pages are served from the cache."""

from sitemap_cache_warmer._version import __version__
from sitemap_cache_warmer.cache_status import BYPASS, HIT, MISS, UNKNOWN, Detection, Signal, detect
from sitemap_cache_warmer.lock import LockError, LockFile
from sitemap_cache_warmer.profiles import BUILTIN_PROFILES, Profile
from sitemap_cache_warmer.report import Summary, summarize
from sitemap_cache_warmer.resolve import ResolveRule, ResolvingTransport
from sitemap_cache_warmer.selection import select
from sitemap_cache_warmer.sitemap import SitemapEntry, parse_sitemap, read_sitemaps
from sitemap_cache_warmer.warmer import RequestResult, UrlResult, Warmer, WarmSettings, make_client

__all__ = [
    "BUILTIN_PROFILES",
    "BYPASS",
    "HIT",
    "MISS",
    "UNKNOWN",
    "Detection",
    "LockError",
    "LockFile",
    "Profile",
    "RequestResult",
    "ResolveRule",
    "ResolvingTransport",
    "Signal",
    "SitemapEntry",
    "Summary",
    "UrlResult",
    "WarmSettings",
    "Warmer",
    "__version__",
    "detect",
    "make_client",
    "parse_sitemap",
    "read_sitemaps",
    "select",
    "summarize",
]

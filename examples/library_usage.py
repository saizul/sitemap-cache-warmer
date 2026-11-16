"""Use sitemap-cache-warmer as a library. Runs offline: a fake Nginx FastCGI cache answers through MockTransport."""

from __future__ import annotations

import asyncio

import httpx

from sitemap_cache_warmer import BUILTIN_PROFILES, Warmer, WarmSettings, make_client, summarize
from sitemap_cache_warmer.report import format_table

stored: set[str] = set()


def origin(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    status = "HIT" if path in stored else "MISS"
    stored.add(path)
    return httpx.Response(200, text="<html><body>ok</body></html>", headers={"x-fastcgi-cache": status})


async def main() -> None:
    settings = WarmSettings(concurrency=2, verify=True)
    urls = [f"https://shop.example.com/{name}/" for name in ("shoes", "bags", "hats")]
    async with make_client(settings, httpx.MockTransport(origin)) as client:
        warmer = Warmer(client, settings, [BUILTIN_PROFILES["desktop"]])
        results = await warmer.run(urls)
    summary = summarize(results, requests=warmer.requests)
    print(format_table(results, summary))
    assert summary.hit_rate == 100.0


if __name__ == "__main__":
    asyncio.run(main())

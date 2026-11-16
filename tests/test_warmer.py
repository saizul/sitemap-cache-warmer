from __future__ import annotations

import asyncio

import httpx
import pytest

from fakecache import HOST, FakeCache, html
from sitemap_cache_warmer.cache_status import BYPASS, HIT, MISS, UNKNOWN
from sitemap_cache_warmer.profiles import BUILTIN_PROFILES, Profile, parse_header, resolve_profiles
from sitemap_cache_warmer.ratelimit import HostRateLimiter
from sitemap_cache_warmer.resolve import ResolveRule, ResolvingTransport
from sitemap_cache_warmer.warmer import Warmer, WarmSettings, make_client

DESKTOP = BUILTIN_PROFILES["desktop"]
MOBILE = BUILTIN_PROFILES["mobile"]
URLS = [f"{HOST}/", f"{HOST}/about/", f"{HOST}/cart/"]


async def _no_sleep(_: float) -> None:
    return None


def run(cache: FakeCache, urls, *, profiles=(DESKTOP,), sleeps: list[float] | None = None, **settings):
    s = WarmSettings(**settings)

    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    async def go():
        async with make_client(s, cache.transport) as client:
            warmer = Warmer(client, s, list(profiles), sleep=sleep)
            return await warmer.run(urls), warmer.requests

    return asyncio.run(go())


class TestWarm:
    def test_single_pass_reports_first_status(self, cache: FakeCache):
        results, requests = run(cache, URLS)
        assert [r.cache_status for r in results] == [MISS, MISS, BYPASS]
        assert requests == 3
        assert all(r.verify is None for r in results)
        first = results[0].warm
        assert first.status_code == 200 and first.ttfb is not None and first.total >= first.ttfb
        assert first.size > 0
        assert first.detection.source == "x-fastcgi-cache"

    def test_two_pass_verifies_hits(self, cache: FakeCache):
        results, requests = run(cache, URLS, verify=True)
        assert [(r.warm.detection.status, r.verify.detection.status) for r in results] == [
            (MISS, HIT),
            (MISS, HIT),
            (BYPASS, BYPASS),
        ]
        assert [r.cache_status for r in results] == [HIT, HIT, BYPASS]
        assert requests == 6

    def test_profiles_warm_each_variant(self, cache: FakeCache):
        results, _ = run(cache, [f"{HOST}/"], profiles=(DESKTOP, MOBILE), verify=True)
        assert [(r.profile, r.warm.detection.status, r.verify.detection.status) for r in results] == [
            ("desktop", MISS, HIT),
            ("mobile", MISS, HIT),
        ]
        uas = {r.headers["user-agent"] for r in cache.requests}
        assert any("Mobile" in ua for ua in uas) and any("Windows" in ua for ua in uas)
        assert all(r.headers["accept-encoding"] == "gzip, deflate, br" for r in cache.requests)
        assert all(r.headers["accept"].startswith("text/html") for r in cache.requests)

    def test_extra_headers_override_profile(self, cache: FakeCache):
        run(cache, [f"{HOST}/"], extra_headers={"user-agent": "Custom/1.0", "X-Warmup": "1"})
        request = cache.requests[0]
        assert request.headers["user-agent"] == "Custom/1.0"
        assert request.headers["x-warmup"] == "1"

    def test_cookies_are_never_sent_back(self, cache: FakeCache):
        def set_cookie(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="x", headers={"set-cookie": "woocommerce_session=abc; Path=/"})

        cache.overrides["/session/"] = set_cookie
        run(cache, [f"{HOST}/session/", f"{HOST}/"], concurrency=1)
        assert "cookie" not in cache.requests[-1].headers

    def test_errors_and_404s_are_not_verified(self, cache: FakeCache):
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        cache.overrides["/down/"] = boom
        results, _ = run(cache, [f"{HOST}/down/", f"{HOST}/nope/"], verify=True, retries=0)
        down, nope = results
        assert down.cache_status == "error" and "ConnectError" in down.warm.error and down.verify is None
        assert nope.warm.status_code == 404 and nope.verify is None

    def test_retries_with_backoff_and_retry_after(self, cache: FakeCache):
        calls = {"n": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503, headers={"retry-after": "7"})
            if calls["n"] == 2:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, text="ok", headers={"x-fastcgi-cache": "MISS"})

        cache.overrides["/flaky/"] = flaky
        sleeps: list[float] = []
        results, _ = run(cache, [f"{HOST}/flaky/"], retries=3, backoff=0.5, sleeps=sleeps)
        result = results[0].warm
        assert result.ok and result.attempts == 3 and result.error == ""
        assert sleeps == [7.0, 1.0]  # Retry-After, then backoff * 2

    def test_retries_exhausted(self, cache: FakeCache):
        cache.overrides["/busy/"] = lambda r: httpx.Response(429)
        sleeps: list[float] = []
        results, _ = run(cache, [f"{HOST}/busy/"], retries=2, backoff=1, sleeps=sleeps)
        assert results[0].warm.status_code == 429 and results[0].warm.attempts == 3
        assert sleeps == [1, 2]

    def test_timeout_message(self, cache: FakeCache):
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        cache.overrides["/slow/"] = slow
        results, _ = run(cache, [f"{HOST}/slow/"], retries=0)
        assert results[0].warm.error == "timeout (ConnectTimeout)"

    def test_redirects_not_followed_by_default(self, cache: FakeCache):
        cache.overrides["/old/"] = lambda r: httpx.Response(301, headers={"location": f"{HOST}/about/"})
        results, _ = run(cache, [f"{HOST}/old/"], verify=True)
        assert results[0].warm.is_redirect and results[0].warm.location == f"{HOST}/about/"
        assert results[0].verify is None
        results, _ = run(cache, [f"{HOST}/old/"], follow_redirects=True)
        assert results[0].warm.status_code == 200

    def test_verify_delay(self, cache: FakeCache):
        sleeps: list[float] = []
        run(cache, [f"{HOST}/"], verify=True, verify_delay=2.5, sleeps=sleeps)
        assert sleeps == [2.5]

    def test_layer_option(self):
        cache = FakeCache(style="cloudflare")
        results, _ = run(cache, [f"{HOST}/"], layer="nginx")
        assert results[0].cache_status == UNKNOWN
        results, _ = run(FakeCache(style="cloudflare"), [f"{HOST}/cart/"])
        assert results[0].cache_status == BYPASS  # cf-cache-status: DYNAMIC

    @pytest.mark.parametrize(
        ("style", "cart"),
        # Varnish shows a pass like a miss (one XID, Age 0) unless the VCL adds its own header.
        [("cloudflare", BYPASS), ("varnish", MISS), ("litespeed", BYPASS)],
    )
    def test_other_cache_styles(self, style, cart):
        results, _ = run(FakeCache(style=style), URLS, verify=True)
        assert [r.cache_status for r in results] == [HIT, HIT, cart]

    def test_footer_fingerprint_two_pass(self):
        stamp = "<!-- Cached page generated" + " by WP-Super-Cache on 2026-11-16 10:00:00 -->"
        cache = FakeCache(style="none", footer=lambda path, hit: stamp)
        results, _ = run(cache, [f"{HOST}/about/"], verify=True)
        assert results[0].warm.detection.status == UNKNOWN
        assert results[0].cache_status == HIT
        assert "unchanged" in results[0].verify.detection.raw

    def test_footer_only_at_the_end_of_large_pages(self, cache: FakeCache):
        big = html("x" * 200_000, "<!-- Cache Enabler by KeyCDN @ Mon, 16 Nov 2026 10:00:00 GMT -->")
        cache.overrides["/big/"] = lambda r: httpx.Response(200, text=big, headers={"content-type": "text/html"})
        results, _ = run(cache, [f"{HOST}/big/"], tail_bytes=4096)
        assert results[0].cache_status == HIT
        assert results[0].warm.size == len(big)

    def test_brotli_body_is_decoded(self, cache: FakeCache):
        brotli = pytest.importorskip("brotli")
        body = html("br", "<!-- Cache Enabler by KeyCDN @ Mon, 16 Nov 2026 10:00:00 GMT -->").encode()
        cache.overrides["/br/"] = lambda r: httpx.Response(
            200, content=brotli.compress(body), headers={"content-encoding": "br", "content-type": "text/html"}
        )
        results, _ = run(cache, [f"{HOST}/br/"])
        assert results[0].cache_status == HIT

    def test_non_html_body_not_inspected(self, cache: FakeCache):
        marker = "<!-- Cache Enabler by KeyCDN @ Mon, 16 Nov 2026 10:00:00 GMT -->"
        cache.overrides["/x.json/"] = lambda r: httpx.Response(
            200, text=marker, headers={"content-type": "application/json"}
        )
        results, _ = run(cache, [f"{HOST}/x.json/"])
        assert results[0].cache_status == UNKNOWN

    def test_concurrency_limit(self, cache: FakeCache):
        active = {"now": 0, "max": 0}

        class Counting(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
                await asyncio.sleep(0.01)
                active["now"] -= 1
                return httpx.Response(200, text="ok")

        s = WarmSettings(concurrency=3)

        async def go():
            async with make_client(s, Counting()) as client:
                await Warmer(client, s, [DESKTOP]).run([f"{HOST}/p{i}" for i in range(12)])

        asyncio.run(go())
        assert active["max"] == 3

    def test_invalid_settings(self, cache: FakeCache):
        with pytest.raises(ValueError):
            run(cache, URLS, concurrency=0)
        with pytest.raises(ValueError):
            asyncio.run(_warmer_without_profiles())


async def _warmer_without_profiles():
    async with httpx.AsyncClient() as client:
        Warmer(client, WarmSettings(), [])


class TestRateLimiter:
    def test_spacing_per_host(self):
        clock = {"t": 0.0}
        waits: list[float] = []

        async def sleep(seconds: float) -> None:
            waits.append(seconds)

        limiter = HostRateLimiter(rate=2, clock=lambda: clock["t"], sleep=sleep)

        async def go():
            return [await limiter.wait("a"), await limiter.wait("a"), await limiter.wait("b"), await limiter.wait("a")]

        assert asyncio.run(go()) == [0.0, 0.5, 0.0, 1.0]
        assert waits == [0.5, 1.0]

    def test_delay_wins_when_larger(self):
        assert HostRateLimiter(rate=10, delay=1.5).interval == 1.5
        assert HostRateLimiter().interval == 0

    def test_unlimited_does_not_wait(self):
        assert asyncio.run(HostRateLimiter().wait("a")) == 0.0

    def test_negative(self):
        with pytest.raises(ValueError):
            HostRateLimiter(rate=-1)


class TestResolve:
    def test_parse(self):
        assert ResolveRule.parse("Shop.Example.com:443:203.0.113.10") == ResolveRule(
            "shop.example.com", 443, "203.0.113.10"
        )
        assert ResolveRule.parse("x.io:80:[2001:db8::1]").address == "2001:db8::1"

    @pytest.mark.parametrize(
        "bad", ["x.io:443", "x.io:http:1.2.3.4", "x.io:0:1.2.3.4", "x.io:443:not-an-ip", ":443:1.2.3.4"]
    )
    def test_parse_errors(self, bad):
        with pytest.raises(ValueError):
            ResolveRule.parse(bad)

    def test_rewrites_target_keeps_host_and_sni(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text="ok")

        transport = ResolvingTransport(
            httpx.MockTransport(handler),
            [ResolveRule("shop.example.com", 443, "203.0.113.10"), ResolveRule("shop.example.com", 80, "2001:db8::1")],
        )

        async def go():
            async with httpx.AsyncClient(transport=transport) as client:
                await client.get("https://shop.example.com/a?b=1")
                await client.get("http://shop.example.com/c")
                await client.get("https://other.example.com/")

        asyncio.run(go())
        first, second, third = seen
        assert first.url.host == "203.0.113.10" and first.url.path == "/a" and first.url.query == b"b=1"
        assert first.headers["host"] == "shop.example.com"
        assert first.extensions["sni_hostname"] == "shop.example.com"
        assert second.url.host == "2001:db8::1" and "sni_hostname" not in second.extensions
        assert second.headers["host"] == "shop.example.com"
        assert third.url.host == "other.example.com"


class TestProfiles:
    def test_merge_is_case_insensitive(self):
        p = Profile("p", {"User-Agent": "a", "Accept": "b"})
        assert p.merged({"user-agent": "c"}) == {"Accept": "b", "user-agent": "c"}

    def test_parse_header(self):
        assert parse_header("X-Warmup:  yes ") == ("X-Warmup", "yes")
        for bad in ("novalue", ": x", "Bad Name: x"):
            with pytest.raises(ValueError):
                parse_header(bad)

    def test_resolve_profiles(self):
        custom = {"tablet": Profile("tablet", {"User-Agent": "iPad"})}
        assert [p.name for p in resolve_profiles(["desktop", "tablet", "desktop"], custom)] == ["desktop", "tablet"]
        with pytest.raises(ValueError, match="unknown profile"):
            resolve_profiles(["watch"])

from __future__ import annotations

import pytest

from sitemap_cache_warmer.cache_status import (
    BYPASS,
    HIT,
    MISS,
    UNKNOWN,
    Detection,
    compare_fingerprints,
    detect,
)

# The WP Super Cache footer, split so the phrase is assembled at runtime.
WPSC_CACHED = "<!-- Cached page generated" + " by WP-Super-Cache on 2026-11-16 10:00:00 -->"


@pytest.mark.parametrize(
    ("value", "status"),
    [
        ("HIT", HIT),
        ("MISS", MISS),
        ("EXPIRED", MISS),
        ("STALE", HIT),
        ("UPDATING", HIT),
        ("REVALIDATED", HIT),
        ("BYPASS", BYPASS),
        ("DYNAMIC", BYPASS),
        ("NONE", UNKNOWN),
        ("unknown", UNKNOWN),
        (" hit ", HIT),
    ],
)
def test_cloudflare(value, status):
    d = detect({"CF-Cache-Status": value})
    assert d.status == status
    if status != UNKNOWN:
        assert (d.layer, d.source) == ("cdn", "cf-cache-status")


@pytest.mark.parametrize(
    ("value", "status"),
    [
        ("Hit from cloudfront", HIT),
        ("Miss from cloudfront", MISS),
        ("RefreshHit from cloudfront", HIT),
        ("Error from cloudfront", UNKNOWN),
        ("MISS, HIT", HIT),  # Fastly shield + edge
        ("MISS, MISS", MISS),
        ("TCP_MEM_HIT from a23-1-2-3.deploy.akamaitechnologies.com", HIT),
        ("TCP_MISS from a23-1-2-3.deploy.akamaitechnologies.com", MISS),
        ("TCP_REFRESH_MISS", MISS),
        ("PASS", BYPASS),
    ],
)
def test_x_cache(value, status):
    assert detect({"X-Cache": value}).status == status


@pytest.mark.parametrize("name", ["x-fastcgi-cache", "x-nginx-cache", "x-proxy-cache", "x-cache-status"])
@pytest.mark.parametrize(("value", "status"), [("HIT", HIT), ("MISS", MISS), ("BYPASS", BYPASS), ("EXPIRED", MISS)])
def test_nginx_headers(name, value, status):
    d = detect({name: value})
    assert (d.status, d.layer, d.source) == (status, "nginx", name)


def test_varnish():
    assert detect({"X-Varnish": "32770 32768", "Age": "30"}).status == HIT
    d = detect({"X-Varnish": "32770", "Age": "0"})
    assert (d.status, d.layer) == (MISS, "varnish")


def test_varnish_x_cache_is_varnish_layer():
    d = detect({"X-Cache": "HIT", "X-Varnish": "5 3", "Via": "1.1 varnish"})
    assert d.layer == "varnish"


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"X-LiteSpeed-Cache": "hit"}, HIT),
        ({"X-LiteSpeed-Cache": "hit,private"}, HIT),
        ({"X-LiteSpeed-Cache": "miss"}, MISS),
        ({"X-LiteSpeed-Cache-Control": "no-cache"}, BYPASS),
    ],
)
def test_litespeed(headers, status):
    d = detect(headers)
    assert (d.status, d.layer) == (status, "litespeed")


def test_age_is_a_last_resort():
    assert detect({"Age": "120"}).status == HIT
    assert detect({"Age": "120"}).layer == "age"
    assert detect({"Age": "0"}).status == UNKNOWN
    assert detect({"Age": "x"}).status == UNKNOWN
    # A real cache header wins over Age.
    assert detect({"Age": "120", "X-FastCGI-Cache": "MISS"}).status == MISS


def test_cdn_wins_over_origin_in_auto_mode():
    headers = {"CF-Cache-Status": "HIT", "X-FastCGI-Cache": "MISS"}
    assert detect(headers).status == HIT
    assert detect(headers, layer="nginx").status == MISS
    assert detect(headers, layer="litespeed").status == UNKNOWN


def test_unknown_cdn_value_falls_through_to_origin():
    d = detect({"CF-Cache-Status": "NONE", "X-FastCGI-Cache": "HIT"})
    assert (d.status, d.layer) == (HIT, "nginx")


def test_nothing_detected():
    d = detect({"content-type": "text/html"}, "<html></html>")
    assert d == Detection()


class TestPluginHeaders:
    def test_wp_super_cache_header(self):
        d = detect({"WP-Super-Cache": "Served supercache file from PHP"})
        assert (d.status, d.plugin) == (HIT, "WP Super Cache")

    def test_cache_enabler_header(self):
        assert detect({"X-Cache-Handler": "cache-enabler-engine"}).status == HIT

    def test_wp_rocket_nginx_bypass(self):
        assert detect({"X-Rocket-Nginx-Bypass": "Yes"}).status == BYPASS


class TestBodyMarkers:
    def test_cache_enabler(self):
        body = "<html></html>\n<!-- Cache Enabler by KeyCDN @ Mon, 16 Nov 2026 10:00:00 GMT (https gzip webp) -->"
        d = detect({}, body)
        assert (d.status, d.layer, d.plugin) == (HIT, "plugin", "Cache Enabler")
        assert d.fingerprint == "Mon, 16 Nov 2026 10:00:00 GMT"

    def test_wp_super_cache_cached_comment_is_ambiguous(self):
        body = "<!-- Dynamic page generated in 0.311 seconds. -->\n" + WPSC_CACHED
        d = detect({}, body)
        assert d.status == UNKNOWN
        assert d.plugin == "WP Super Cache"
        assert d.fingerprint == "2026-11-16 10:00:00"

    def test_wp_super_cache_supercache_comment(self):
        assert detect({}, WPSC_CACHED + "\n<!-- super cache -->").status == HIT

    def test_wp_super_cache_dynamic_only_is_miss(self):
        d = detect({}, "<html></html><!-- Dynamic page generated in 0.311 seconds. -->")
        assert (d.status, d.plugin) == (MISS, "WP Super Cache")

    def test_w3_total_cache(self):
        footer = (
            "<!--\nPerformance optimized by W3 Total Cache.\n\n"
            "Page Caching using Disk: Enhanced \n\n"
            "Served from: shop.example.com @ 2026-11-16 10:00:00 by W3 Total Cache\n-->"
        )
        d = detect({}, footer)
        assert d.status == UNKNOWN and d.plugin == "W3 Total Cache"
        assert d.fingerprint == "2026-11-16 10:00:00"
        rejected = footer.replace("Enhanced ", "Enhanced (Requested URI is rejected) ")
        assert detect({}, rejected).status == BYPASS

    def test_wp_fastest_cache(self):
        body = "<!-- WP Fastest Cache file was created in 0.42 seconds, on 16-11-26 10:00:00 -->"
        d = detect({}, body)
        assert (d.status, d.plugin, d.fingerprint) == (HIT, "WP Fastest Cache", "16-11-26 10:00:00")

    def test_wp_rocket(self):
        body = "<!-- This website is like a Rocket, isn't it? -->\n<!-- Debug: cached@1763287200 -->"
        assert detect({}, body).fingerprint == "1763287200"

    def test_hummingbird_and_breeze(self):
        assert detect({}, "<!-- Hummingbird cache file was created in 1.2 seconds, on 16-11-26 10:00 -->").plugin == (
            "Hummingbird"
        )
        assert detect({}, "<!-- Cache served by breeze CACHE - Last modified: Mon, 16 Nov 2026 -->").status == HIT

    def test_headers_win_over_body(self):
        body = "<!-- Cache Enabler by KeyCDN @ Mon, 16 Nov 2026 10:00:00 GMT -->"
        assert detect({"X-FastCGI-Cache": "MISS"}, body).status == MISS
        assert detect({"X-FastCGI-Cache": "MISS"}, body, layer="plugin").status == HIT


class TestFingerprints:
    def test_same_timestamp_means_hit(self):
        a = detect({}, WPSC_CACHED)
        b = detect({}, WPSC_CACHED)
        refined = compare_fingerprints(a, b)
        assert refined.status == HIT
        assert "unchanged" in refined.raw

    def test_new_timestamp_means_miss(self):
        a = detect({}, WPSC_CACHED)
        b = detect({}, WPSC_CACHED.replace("10:00:00", "10:05:00"))
        assert compare_fingerprints(a, b).status == MISS

    def test_known_status_is_kept(self):
        a = detect({}, WPSC_CACHED)
        b = detect({"X-FastCGI-Cache": "BYPASS"}, WPSC_CACHED)
        assert compare_fingerprints(a, b).status == BYPASS

    def test_no_fingerprint(self):
        assert compare_fingerprints(Detection(), Detection()).status == UNKNOWN


def test_to_dict():
    data = detect({"X-FastCGI-Cache": "HIT", "Age": "3"}).to_dict()
    assert data["status"] == HIT
    assert [s["source"] for s in data["signals"]] == ["x-fastcgi-cache", "age"]

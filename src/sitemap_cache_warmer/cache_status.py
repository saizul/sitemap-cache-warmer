"""Cache status detection from response headers and HTML footer comments.

Every cache layer that leaves a trace produces a :class:`Signal`. The overall status is taken from the layer
closest to the client (CDN, then Varnish, LiteSpeed, Nginx, then WordPress page-cache plugins), unless a specific
layer is requested. Statuses are normalised to four values:

* ``hit``: served from a cache (including stale, updating and revalidated responses);
* ``miss``: the cache was consulted but had no fresh copy (``MISS``, ``EXPIRED``);
* ``bypass``: the cache deliberately did not store or serve the page (``BYPASS``, ``DYNAMIC``, plugin exclusions);
* ``unknown``: no layer gave a usable answer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

HIT = "hit"
MISS = "miss"
BYPASS = "bypass"
UNKNOWN = "unknown"
STATUSES = (HIT, MISS, BYPASS, UNKNOWN)

# Layer names, from the client towards the origin.
LAYERS = ("cdn", "varnish", "litespeed", "nginx", "plugin", "age")

# Values of Nginx $upstream_cache_status and Cloudflare cf-cache-status.
_NGINX_STYLE = {
    "HIT": HIT,
    "STALE": HIT,
    "UPDATING": HIT,
    "REVALIDATED": HIT,
    "MISS": MISS,
    "EXPIRED": MISS,
    "BYPASS": BYPASS,
    "DYNAMIC": BYPASS,
}


@dataclass(frozen=True)
class Signal:
    layer: str
    source: str  # header name, or "body" for HTML comments
    status: str
    raw: str
    plugin: str = ""
    fingerprint: str = ""  # a timestamp from the cached copy, used to compare two responses


@dataclass
class Detection:
    status: str = UNKNOWN
    layer: str = ""
    source: str = ""
    raw: str = ""
    signals: list[Signal] = field(default_factory=list)

    @property
    def plugin(self) -> str:
        return next((s.plugin for s in self.signals if s.plugin), "")

    @property
    def fingerprint(self) -> str:
        return next((s.fingerprint for s in self.signals if s.fingerprint), "")

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "layer": self.layer,
            "source": self.source,
            "raw": self.raw,
            "signals": [asdict(s) for s in self.signals],
        }


def _token_status(value: str) -> str:
    """Status of a header such as ``x-cache: HIT, MISS`` or ``Hit from cloudfront``.

    In multi-tier setups (for example a shield and an edge node) any hit means the response came from a cache.
    """
    tokens = [t.strip().upper() for t in value.split(",") if t.strip()]
    found: list[str] = []
    for token in tokens:
        word = token.split()[0] if token.split() else ""
        word = word.removeprefix("TCP_").removeprefix("UDP_")
        if "HIT" in word:
            found.append(HIT)
        elif word in ("MISS", "EXPIRED", "REFRESH_MISS"):
            found.append(MISS)
        elif word in ("BYPASS", "PASS", "DYNAMIC"):
            found.append(BYPASS)
        elif word.endswith("_MISS"):
            found.append(MISS)
    if HIT in found:
        return HIT
    if MISS in found:
        return MISS
    if BYPASS in found:
        return BYPASS
    return UNKNOWN


def _nginx_status(value: str) -> str:
    return _NGINX_STYLE.get(value.strip().upper(), UNKNOWN)


def _header_signals(headers: Mapping[str, str]) -> list[Signal]:
    h = {k.lower(): v for k, v in headers.items()}
    signals: list[Signal] = []

    # ---- CDN -------------------------------------------------------------------------------------------------
    if "cf-cache-status" in h:
        signals.append(Signal("cdn", "cf-cache-status", _nginx_status(h["cf-cache-status"]), h["cf-cache-status"]))
    for name in ("cdn-cache", "x-vercel-cache", "x-bunny-cache", "x-sucuri-cache", "x-qc-cache"):
        if name in h:
            signals.append(Signal("cdn", name, _token_status(h[name]), h[name]))
    if "x-cache" in h:
        value = h["x-cache"]
        # x-cache is used by CDNs (CloudFront, Fastly, Akamai, KeyCDN) and by Varnish setups alike.
        via = h.get("via", "").lower()
        layer = "varnish" if ("varnish" in via and "x-varnish" in h and "cloudfront" not in value.lower()) else "cdn"
        signals.append(Signal(layer, "x-cache", _token_status(value), value))

    # ---- Varnish ---------------------------------------------------------------------------------------------
    if "x-varnish" in h:
        ids = h["x-varnish"].replace(",", " ").split()
        # Varnish puts the request XID, and on a hit also the XID of the request that stored the object.
        status = HIT if len(ids) >= 2 else MISS
        signals.append(Signal("varnish", "x-varnish", status, h["x-varnish"]))

    # ---- LiteSpeed -------------------------------------------------------------------------------------------
    if "x-litespeed-cache" in h:
        value = h["x-litespeed-cache"]
        first = value.split(",")[0].strip().lower()
        status = {"hit": HIT, "miss": MISS}.get(first, UNKNOWN)
        signals.append(Signal("litespeed", "x-litespeed-cache", status, value))
    elif "no-cache" in h.get("x-litespeed-cache-control", "").lower():
        value = h["x-litespeed-cache-control"]
        signals.append(Signal("litespeed", "x-litespeed-cache-control", BYPASS, value))

    # ---- Nginx (FastCGI, proxy and SiteGround-style caches) ------------------------------------------------------
    for name in ("x-fastcgi-cache", "x-nginx-cache", "x-proxy-cache", "x-cache-status", "x-srcache-fetch-status"):
        if name in h:
            signals.append(Signal("nginx", name, _nginx_status(h[name]), h[name]))

    # ---- WordPress plugins that send headers ---------------------------------------------------------------------
    if "wp-super-cache" in h:
        value = h["wp-super-cache"]
        status = HIT if "served" in value.lower() else UNKNOWN
        signals.append(Signal("plugin", "wp-super-cache", status, value, plugin="WP Super Cache"))
    if h.get("x-cache-handler", "").lower().startswith("cache-enabler"):
        signals.append(Signal("plugin", "x-cache-handler", HIT, h["x-cache-handler"], plugin="Cache Enabler"))
    if "x-rocket-nginx-bypass" in h:
        value = h["x-rocket-nginx-bypass"]
        status = BYPASS if value.strip().lower() in ("yes", "1", "true") else HIT
        signals.append(Signal("plugin", "x-rocket-nginx-bypass", status, value, plugin="WP Rocket"))

    # ---- Age: a weak signal, only used when nothing else answers ---------------------------------------------------
    if "age" in h:
        try:
            age = int(h["age"].split(",")[0].strip())
        except ValueError:
            age = -1
        if age > 0:
            signals.append(Signal("age", "age", HIT, h["age"]))
    return signals


@dataclass(frozen=True)
class BodyMarker:
    plugin: str
    pattern: re.Pattern[str]
    status: str
    # Group name in ``pattern`` holding a timestamp that stays the same while the cached copy is served.
    fingerprint_group: str = ""


# Plugins append an HTML comment to the pages they cache. The patterns are matched against the end of the body.
BODY_MARKERS: tuple[BodyMarker, ...] = (
    BodyMarker(
        "Cache Enabler",
        re.compile(r"<!--\s*Cache Enabler by KeyCDN @ (?P<ts>[^(>]+?)\s*(?:\([^)]*\))?\s*-->", re.I),
        HIT,
        "ts",
    ),
    BodyMarker(
        "WP Super Cache",
        re.compile(r"<!--\s*Cached page generated\s+by WP-Super-Cache on (?P<ts>[^>]+?)\s*-->", re.I),
        UNKNOWN,  # the same comment is in the response that created the cache file
        "ts",
    ),
    BodyMarker(
        "WP Super Cache",
        re.compile(r"<!--\s*super cache\s*-->", re.I),
        HIT,
    ),
    BodyMarker(
        "WP Fastest Cache",
        re.compile(r"<!--\s*WP Fastest Cache file was created in [\d.]+ seconds, on (?P<ts>[^>]+?)\s*-->", re.I),
        HIT,
        "ts",
    ),
    BodyMarker(
        "WP Rocket",
        re.compile(r"<!--\s*Debug:\s*cached@(?P<ts>\d+)\s*-->", re.I),
        HIT,
        "ts",
    ),
    BodyMarker(
        "Hummingbird",
        re.compile(r"<!--\s*Hummingbird cache file was created in [\d.]+ seconds, on (?P<ts>[^>]+?)\s*-->", re.I),
        HIT,
        "ts",
    ),
    BodyMarker(
        "Breeze",
        re.compile(r"<!--\s*Cache served by breeze CACHE[^>]*?(?:Last modified:\s*(?P<ts>[^>]+?))?\s*-->", re.I),
        HIT,
        "ts",
    ),
    BodyMarker(
        "W3 Total Cache",
        re.compile(r"Page Caching using [^\n(]+?\((?P<reason>[^)]+)\)", re.I),
        BYPASS,
    ),
    BodyMarker(
        "W3 Total Cache",
        re.compile(r"Served from: [^\n@]+@ (?P<ts>[^\n]+?) by W3 Total Cache", re.I),
        UNKNOWN,  # the footer is the same on the response that filled the cache
        "ts",
    ),
)

# WP Super Cache adds this comment to pages it renders through PHP; without a "Cached page" comment it is a miss.
_WPSC_DYNAMIC = re.compile(r"<!--\s*Dynamic page generated in [\d.]+ seconds\.\s*-->", re.I)


def _body_signals(body: str) -> list[Signal]:
    signals: list[Signal] = []
    seen_plugins: set[str] = set()
    for marker in BODY_MARKERS:
        match = marker.pattern.search(body)
        if not match:
            continue
        if marker.plugin in seen_plugins and marker.status == UNKNOWN:
            continue
        seen_plugins.add(marker.plugin)
        fingerprint = ""
        if marker.fingerprint_group and match.groupdict().get(marker.fingerprint_group):
            fingerprint = match.group(marker.fingerprint_group).strip()
        raw = " ".join(match.group(0).split())[:160]
        signals.append(Signal("plugin", "body", marker.status, raw, plugin=marker.plugin, fingerprint=fingerprint))
    if "WP Super Cache" not in seen_plugins and _WPSC_DYNAMIC.search(body):
        raw = " ".join(_WPSC_DYNAMIC.search(body).group(0).split())  # type: ignore[union-attr]
        signals.append(Signal("plugin", "body", MISS, raw, plugin="WP Super Cache"))
    return signals


def detect(headers: Mapping[str, str], body: str | None = None, *, layer: str = "auto") -> Detection:
    """Detect the cache status of one response.

    ``body`` should be the end of the decoded HTML (the plugins put their comments at the very end).
    ``layer`` is ``auto`` or one of :data:`LAYERS` to judge only that layer.
    """
    signals = _header_signals(headers)
    if body:
        signals.extend(_body_signals(body))
    detection = Detection(signals=signals)
    order = LAYERS if layer == "auto" else (layer,)
    for wanted in order:
        candidates = [s for s in signals if s.layer == wanted and s.status != UNKNOWN]
        if candidates:
            # Within a layer a definite answer wins: hit, then bypass, then miss (a bypass explains a miss).
            best = sorted(candidates, key=lambda s: (HIT, BYPASS, MISS).index(s.status))[0]
            detection.status, detection.layer, detection.source, detection.raw = (
                best.status,
                best.layer,
                best.source,
                best.raw,
            )
            return detection
    for wanted in order:
        known = [s for s in signals if s.layer == wanted]
        if known:
            detection.layer, detection.source, detection.raw = known[0].layer, known[0].source, known[0].raw
            break
    return detection


def compare_fingerprints(first: Detection, second: Detection) -> Detection:
    """Refine an ``unknown`` second response using a plugin timestamp seen in both responses.

    When the page-cache comment carries the same timestamp twice, the second response was served from the cache
    file written by the first. A different timestamp means the page was rendered again.
    """
    if second.status != UNKNOWN:
        return second
    a, b = first.fingerprint, second.fingerprint
    if not a or not b:
        return second
    refined = Detection(signals=list(second.signals))
    refined.layer, refined.source = "plugin", "body"
    refined.status = HIT if a == b else MISS
    refined.raw = f"{second.plugin} timestamp {'unchanged' if a == b else 'changed'}: {b}"
    return refined

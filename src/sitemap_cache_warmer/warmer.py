"""The warming engine: concurrent requests with per-host pacing, retries, TTFB timing and cache detection."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from sitemap_cache_warmer.cache_status import UNKNOWN, Detection, compare_fingerprints, detect
from sitemap_cache_warmer.profiles import Profile
from sitemap_cache_warmer.ratelimit import HostRateLimiter
from sitemap_cache_warmer.resolve import ResolveRule, ResolvingTransport

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 502, 503, 504})
WARM = "warm"
VERIFY = "verify"


@dataclass
class WarmSettings:
    concurrency: int = 4
    rate: float = 0.0  # requests per second per host, 0 = unlimited
    delay: float = 0.0  # seconds between two requests to the same host
    timeout: float = 30.0
    connect_timeout: float = 10.0
    retries: int = 2
    backoff: float = 1.0
    max_retry_after: float = 60.0
    follow_redirects: bool = False
    tail_bytes: int = 65_536  # end of the body kept for footer comment detection
    layer: str = "auto"
    verify: bool = False
    verify_delay: float = 0.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    insecure: bool = False

    def validate(self) -> None:
        if not 1 <= self.concurrency <= 256:
            raise ValueError("concurrency must be between 1 and 256")
        if self.timeout <= 0 or self.connect_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if self.retries < 0:
            raise ValueError("retries cannot be negative")


@dataclass
class RequestResult:
    url: str
    profile: str
    phase: str
    status_code: int = 0
    ttfb: float | None = None  # seconds until the response headers arrived
    total: float | None = None  # seconds until the body was read completely
    size: int = 0  # decoded body bytes
    detection: Detection = field(default_factory=Detection)
    error: str = ""
    attempts: int = 0
    location: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and 200 <= self.status_code < 300

    @property
    def is_redirect(self) -> bool:
        return not self.error and 300 <= self.status_code < 400

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "status_code": self.status_code,
            "ttfb_ms": None if self.ttfb is None else round(self.ttfb * 1000, 1),
            "total_ms": None if self.total is None else round(self.total * 1000, 1),
            "bytes": self.size,
            "cache": self.detection.to_dict(),
            "error": self.error,
            "attempts": self.attempts,
            "location": self.location,
        }


@dataclass
class UrlResult:
    url: str
    profile: str
    warm: RequestResult
    verify: RequestResult | None = None

    @property
    def final(self) -> RequestResult:
        return self.verify or self.warm

    @property
    def cache_status(self) -> str:
        final = self.final
        if final.error:
            return "error"
        return final.detection.status

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "profile": self.profile,
            "cache_status": self.cache_status,
            "warm": self.warm.to_dict(),
            "verify": self.verify.to_dict() if self.verify else None,
        }


class NoCookieTransport(httpx.AsyncBaseTransport):
    """Drops ``Set-Cookie`` from responses so the client never stores or sends cookies.

    A session cookie set by one page (WooCommerce, a consent banner, a login check) would make every later request
    look like a cart or logged-in visitor, and most page caches bypass those.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if "set-cookie" in response.headers:
            del response.headers["set-cookie"]
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def make_client(
    settings: WarmSettings,
    transport: httpx.AsyncBaseTransport | None = None,
    resolve: list[ResolveRule] | None = None,
) -> httpx.AsyncClient:
    """The HTTP client used for warming. Tests pass an ``httpx.MockTransport`` as ``transport``."""
    base = transport or httpx.AsyncHTTPTransport(verify=not settings.insecure, retries=0)
    if resolve:
        base = ResolvingTransport(base, resolve)
    return httpx.AsyncClient(
        transport=NoCookieTransport(base),
        timeout=httpx.Timeout(settings.timeout, connect=settings.connect_timeout),
        follow_redirects=settings.follow_redirects,
        limits=httpx.Limits(max_connections=settings.concurrency * 2, max_keepalive_connections=settings.concurrency),
        trust_env=False,
    )


def _retry_after(response: httpx.Response, cap: float) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    if value.isdigit():
        return min(float(value), cap)
    return None


class Warmer:
    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: WarmSettings,
        profiles: list[Profile],
        *,
        limiter: HostRateLimiter | None = None,
        sleep: Callable[[float], object] = asyncio.sleep,
        on_result: Callable[[RequestResult], None] | None = None,
    ) -> None:
        settings.validate()
        if not profiles:
            raise ValueError("at least one profile is needed")
        self.client = client
        self.settings = settings
        self.profiles = profiles
        self.limiter = limiter or HostRateLimiter(settings.rate, settings.delay)
        self._sem = asyncio.Semaphore(settings.concurrency)
        self._sleep = sleep
        self._on_result = on_result
        self.requests = 0

    async def fetch(self, url: str, profile: Profile, phase: str = WARM) -> RequestResult:
        result = RequestResult(url, profile.name, phase)
        headers = profile.merged(self.settings.extra_headers)
        host = httpx.URL(url).host
        for attempt in range(self.settings.retries + 1):
            result.attempts = attempt + 1
            async with self._sem:
                await self.limiter.wait(host)
                retry_wait = await self._once(url, headers, result)
            if retry_wait is None:
                break
            if attempt < self.settings.retries:
                log.debug("retrying %s in %.1fs (%s)", url, retry_wait, result.error or result.status_code)
                await self._sleep(retry_wait)  # type: ignore[misc]
        if self._on_result is not None:
            self._on_result(result)
        return result

    async def _once(self, url: str, headers: dict[str, str], result: RequestResult) -> float | None:
        """One request; returns the delay before a retry, or ``None`` when the result is final."""
        backoff = self.settings.backoff * (2 ** (result.attempts - 1))
        started = time.perf_counter()
        self.requests += 1
        result.error = ""
        try:
            async with self.client.stream("GET", url, headers=headers) as response:
                result.ttfb = time.perf_counter() - started
                result.status_code = response.status_code
                result.location = response.headers.get("location", "")
                tail = bytearray()
                size = 0
                inspect_body = "html" in response.headers.get("content-type", "html").lower()
                try:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if inspect_body:
                            tail.extend(chunk)
                            if len(tail) > self.settings.tail_bytes:
                                del tail[: len(tail) - self.settings.tail_bytes]
                except httpx.DecodingError:
                    # A body that cannot be decoded: the headers still tell the cache status.
                    inspect_body = False
                result.total = time.perf_counter() - started
                result.size = size
                body = tail.decode("utf-8", errors="replace") if inspect_body else None
                result.detection = detect(response.headers, body, layer=self.settings.layer)
                if response.status_code in RETRY_STATUSES:
                    return _retry_after(response, self.settings.max_retry_after) or backoff
                return None
        except httpx.TimeoutException as exc:
            result.error = f"timeout ({type(exc).__name__})"
        except httpx.HTTPError as exc:
            result.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        result.total = time.perf_counter() - started
        return backoff

    async def run(self, urls: list[str]) -> list[UrlResult]:
        """Warm every URL with every profile, then (with ``settings.verify``) request them again to check."""
        jobs = [(url, profile) for url in urls for profile in self.profiles]
        warmed = await asyncio.gather(*(self.fetch(url, profile, WARM) for url, profile in jobs))
        results = [UrlResult(url, profile.name, res) for (url, profile), res in zip(jobs, warmed, strict=True)]
        if not self.settings.verify:
            return results
        if self.settings.verify_delay > 0:
            await self._sleep(self.settings.verify_delay)  # type: ignore[misc]
        to_verify = [(i, jobs[i]) for i, r in enumerate(results) if r.warm.ok]
        verified = await asyncio.gather(*(self.fetch(url, profile, VERIFY) for _, (url, profile) in to_verify))
        for (index, _), res in zip(to_verify, verified, strict=True):
            if res.detection.status == UNKNOWN:
                res.detection = compare_fingerprints(results[index].warm.detection, res.detection)
            results[index].verify = res
        return results

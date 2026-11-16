"""Summaries and output formats: a text table, JSON and CSV."""

from __future__ import annotations

import csv
import io
import json
import math
import statistics
from dataclasses import dataclass, field

from sitemap_cache_warmer.cache_status import BYPASS, HIT, MISS, UNKNOWN
from sitemap_cache_warmer.warmer import RequestResult, UrlResult


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile (``pct`` from 0 to 100)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(pct / 100 * len(ordered))))
    return ordered[rank - 1]


@dataclass
class TimingStats:
    count: int = 0
    mean: float | None = None
    median: float | None = None
    p95: float | None = None
    max: float | None = None

    @classmethod
    def of(cls, values: list[float]) -> TimingStats:
        if not values:
            return cls()
        return cls(
            len(values), statistics.fmean(values), statistics.median(values), percentile(values, 95), max(values)
        )

    def to_dict(self) -> dict:
        def ms(v: float | None) -> float | None:
            return None if v is None else round(v * 1000, 1)

        return {
            "count": self.count,
            "mean_ms": ms(self.mean),
            "median_ms": ms(self.median),
            "p95_ms": ms(self.p95),
            "max_ms": ms(self.max),
        }


@dataclass
class Summary:
    urls: int = 0
    results: int = 0
    requests: int = 0
    two_pass: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    http_errors: int = 0
    redirects: int = 0
    errors: int = 0
    hit_rate: float | None = None
    unknown_as_miss: bool = False
    warm_ttfb: TimingStats = field(default_factory=TimingStats)
    final_ttfb: TimingStats = field(default_factory=TimingStats)
    slowest: list[UrlResult] = field(default_factory=list)
    duration: float = 0.0
    by_profile: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "urls": self.urls,
            "results": self.results,
            "requests": self.requests,
            "two_pass": self.two_pass,
            "cache_status": self.counts,
            "by_profile": self.by_profile,
            "hit_rate": None if self.hit_rate is None else round(self.hit_rate, 2),
            "unknown_as_miss": self.unknown_as_miss,
            "http_errors": self.http_errors,
            "redirects": self.redirects,
            "errors": self.errors,
            "ttfb_warm": self.warm_ttfb.to_dict(),
            "ttfb_final": self.final_ttfb.to_dict(),
            "slowest": [
                {"url": r.url, "profile": r.profile, "ttfb_ms": round((r.final.ttfb or 0) * 1000, 1)}
                for r in self.slowest
            ],
            "duration_s": round(self.duration, 2),
        }


def summarize(
    results: list[UrlResult],
    *,
    requests: int = 0,
    slowest: int = 10,
    unknown_as_miss: bool = False,
    duration: float = 0.0,
) -> Summary:
    """Counts and timings. The hit rate is computed from the last pass, over 2xx responses whose cache status is
    known (``hit``, ``miss`` or ``bypass``); with ``unknown_as_miss`` unknown statuses count as misses."""
    summary = Summary(
        urls=len({r.url for r in results}),
        results=len(results),
        requests=requests,
        two_pass=any(r.verify is not None for r in results),
        unknown_as_miss=unknown_as_miss,
        duration=duration,
    )
    counts = {HIT: 0, MISS: 0, BYPASS: 0, UNKNOWN: 0, "error": 0}
    for r in results:
        final = r.final
        profile_counts = summary.by_profile.setdefault(r.profile, {HIT: 0, MISS: 0, BYPASS: 0, UNKNOWN: 0, "error": 0})
        if final.error:
            summary.errors += 1
            counts["error"] += 1
            profile_counts["error"] += 1
            continue
        if final.is_redirect:
            summary.redirects += 1
        elif final.status_code >= 400:
            summary.http_errors += 1
        status = final.detection.status
        counts[status] += 1
        profile_counts[status] += 1
    summary.counts = counts

    eligible = [r for r in results if r.final.ok]
    hits = sum(1 for r in eligible if r.final.detection.status == HIT)
    known = [r for r in eligible if unknown_as_miss or r.final.detection.status != UNKNOWN]
    summary.hit_rate = (100.0 * hits / len(known)) if known else None

    summary.warm_ttfb = TimingStats.of([r.warm.ttfb for r in results if r.warm.ttfb is not None and not r.warm.error])
    summary.final_ttfb = TimingStats.of(
        [r.final.ttfb for r in results if r.final.ttfb is not None and not r.final.error]
    )
    timed = [r for r in results if r.final.ttfb is not None and not r.final.error]
    summary.slowest = sorted(timed, key=lambda r: r.final.ttfb or 0, reverse=True)[:slowest]
    return summary


# ---- formats -------------------------------------------------------------------------------------------------------
def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value * 1000:.0f}"


def _cell(result: RequestResult | None) -> str:
    if result is None:
        return "-"
    if result.error:
        return "error"
    return result.detection.status


def format_table(results: list[UrlResult], summary: Summary, *, show_rows: bool = True, width: int = 70) -> str:
    lines: list[str] = []
    two_pass = summary.two_pass
    if show_rows and results:
        header = f"{'URL':<{width}}  {'PROFILE':<8} {'CODE':>4} {'WARM':<8} {'TTFB':>6}"
        if two_pass:
            header += f" {'VERIFY':<8} {'TTFB':>6}"
        header += "  SOURCE"
        lines.append(header)
        for r in results:
            url = r.url if len(r.url) <= width else r.url[: width - 1] + "…"
            code = str(r.final.status_code or "-")
            row = f"{url:<{width}}  {r.profile:<8} {code:>4} {_cell(r.warm):<8} {_ms(r.warm.ttfb):>6}"
            if two_pass:
                row += f" {_cell(r.verify):<8} {_ms(r.verify.ttfb if r.verify else None):>6}"
            final = r.final
            source = final.error or (
                f"{final.detection.source}: {final.detection.raw}" if final.detection.source else ""
            )
            if final.is_redirect and final.location:
                source = f"redirect -> {final.location}"
            row += f"  {source[:60]}"
            lines.append(row.rstrip())
        lines.append("")
    c = summary.counts
    lines.append(
        f"{summary.urls} URL(s), {summary.results} result(s), {summary.requests} request(s) in {summary.duration:.1f}s"
    )
    lines.append(
        f"cache ({'verify pass' if two_pass else 'single pass'}): {c.get(HIT, 0)} hit, {c.get(MISS, 0)} miss, "
        f"{c.get(BYPASS, 0)} bypass, {c.get(UNKNOWN, 0)} unknown, {c.get('error', 0)} error"
    )
    if summary.http_errors or summary.redirects:
        lines.append(f"HTTP: {summary.http_errors} error status(es), {summary.redirects} redirect(s)")
    rate = "n/a (no cache status detected)" if summary.hit_rate is None else f"{summary.hit_rate:.1f}%"
    lines.append(f"hit rate: {rate}")
    w, f = summary.warm_ttfb, summary.final_ttfb
    lines.append(f"TTFB warm: median {_ms(w.median)} ms, p95 {_ms(w.p95)} ms, max {_ms(w.max)} ms")
    if two_pass:
        lines.append(f"TTFB verify: median {_ms(f.median)} ms, p95 {_ms(f.p95)} ms, max {_ms(f.max)} ms")
    if summary.slowest:
        lines.append("slowest:")
        for r in summary.slowest:
            lines.append(f"  {_ms(r.final.ttfb):>6} ms  {r.final.detection.status:<7} {r.profile:<8} {r.url}")
    return "\n".join(lines) + "\n"


def format_json(results: list[UrlResult], summary: Summary) -> str:
    return json.dumps({"summary": summary.to_dict(), "results": [r.to_dict() for r in results]}, indent=2) + "\n"


CSV_COLUMNS = [
    "url",
    "profile",
    "cache_status",
    "status_code",
    "warm_status",
    "warm_ttfb_ms",
    "warm_total_ms",
    "verify_status",
    "verify_ttfb_ms",
    "verify_total_ms",
    "layer",
    "source",
    "raw",
    "plugin",
    "bytes",
    "attempts",
    "error",
    "location",
]


def _safe(value: object) -> object:
    # Spreadsheet programs run cells that start with these characters as formulas.
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def format_csv(results: list[UrlResult]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()

    def ms(v: float | None) -> str:
        return "" if v is None else f"{v * 1000:.1f}"

    for r in results:
        final = r.final
        row = {
            "url": r.url,
            "profile": r.profile,
            "cache_status": r.cache_status,
            "status_code": final.status_code or "",
            "warm_status": _cell(r.warm),
            "warm_ttfb_ms": ms(r.warm.ttfb),
            "warm_total_ms": ms(r.warm.total),
            "verify_status": _cell(r.verify) if r.verify else "",
            "verify_ttfb_ms": ms(r.verify.ttfb) if r.verify else "",
            "verify_total_ms": ms(r.verify.total) if r.verify else "",
            "layer": final.detection.layer,
            "source": final.detection.source,
            "raw": final.detection.raw,
            "plugin": final.detection.plugin,
            "bytes": final.size,
            "attempts": final.attempts,
            "error": final.error,
            "location": final.location,
        }
        writer.writerow({k: _safe(v) for k, v in row.items()})
    return buffer.getvalue()

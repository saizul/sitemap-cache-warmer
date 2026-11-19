"""Command-line interface: ``warm``, ``urls`` and ``check``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from pathlib import Path

import httpx

from sitemap_cache_warmer._version import __version__
from sitemap_cache_warmer.cache_status import LAYERS
from sitemap_cache_warmer.config import FORMATS, Config, ConfigError, load_config
from sitemap_cache_warmer.lock import LockError, LockFile
from sitemap_cache_warmer.profiles import BUILTIN_PROFILES, TOOL_UA, parse_header, resolve_profiles
from sitemap_cache_warmer.report import format_csv, format_json, format_table, summarize
from sitemap_cache_warmer.resolve import ResolvingTransport
from sitemap_cache_warmer.selection import ORDERS, select
from sitemap_cache_warmer.sitemap import SitemapEntry, read_sitemaps, read_url_list
from sitemap_cache_warmer.warmer import RequestResult, Warmer, WarmSettings, make_client

log = logging.getLogger("sitemap_cache_warmer")

EXIT_OK = 0
EXIT_THRESHOLD = 1  # hit rate below --min-hit-rate, or more errors than --max-errors
EXIT_USAGE = 2  # bad arguments or configuration
EXIT_NO_URLS = 3  # no sitemap could be read, or no URL left after filtering
EXIT_LOCKED = 4  # another run holds the lock file


def _common(p: argparse.ArgumentParser, *, selection: bool = True) -> None:
    p.add_argument("-c", "--config", help="TOML configuration file")
    if selection:
        g = p.add_argument_group("URL selection")
        g.add_argument(
            "--urls",
            action="append",
            metavar="FILE",
            help="file with one URL per line, '-' for standard input (repeatable; with or without sitemaps)",
        )
        g.add_argument("--include", action="append", metavar="REGEX", help="only URLs matching (repeatable)")
        g.add_argument("--exclude", action="append", metavar="REGEX", help="skip URLs matching (repeatable)")
        g.add_argument("--order", choices=ORDERS, help="warm order (default: sitemap)")
        g.add_argument("--limit", type=int, help="warm at most N URLs")
        g.add_argument("--since", help="only URLs with lastmod after a date (2026-11-01) or within an age (7d, 36h)")


def _request_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("requests")
    g.add_argument(
        "--profile",
        action="append",
        metavar="NAME",
        help=f"header profile, repeatable or comma separated: {', '.join(BUILTIN_PROFILES)} or one from the config",
    )
    g.add_argument("-H", "--header", action="append", metavar="'NAME: VALUE'", help="extra request header")
    g.add_argument(
        "--resolve", action="append", metavar="HOST:PORT:ADDR", help="connect to ADDR for HOST:PORT (like curl)"
    )
    g.add_argument("--concurrency", type=int, help="parallel requests (default 4)")
    g.add_argument("--rate", type=float, help="maximum requests per second per host (default unlimited)")
    g.add_argument("--delay", type=float, help="seconds between requests to the same host (default 0)")
    g.add_argument("--timeout", type=float, help="total timeout per request in seconds (default 30)")
    g.add_argument("--connect-timeout", type=float, help="connect timeout in seconds (default 10)")
    g.add_argument("--retries", type=int, help="retries on network errors, 429, 502, 503, 504 (default 2)")
    g.add_argument("--follow-redirects", action="store_true", default=None, help="follow redirects")
    g.add_argument("--insecure", action="store_true", default=None, help="do not verify TLS certificates")
    g.add_argument("--layer", choices=["auto", *LAYERS], help="judge only this cache layer (default auto)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sitemap-cache-warmer",
        description="Warm page caches (CDN, Varnish, Nginx FastCGI, LiteSpeed, WordPress plugins) from sitemaps.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="log every request (-vv for debug)")
    parser.add_argument("-q", "--quiet", action="store_true", help="no output except errors and the report file")
    # -v and -q also work after the command name ("warm -q").
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    shared.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    w = sub.add_parser("warm", parents=[shared], help="request every sitemap URL so the caches store it")
    w.add_argument("sitemaps", nargs="*", metavar="SITEMAP", help="sitemap URL or local file (index, urlset, .gz)")
    _common(w)
    _request_options(w)
    g = w.add_argument_group("verification and output")
    g.add_argument("--verify", action="store_true", default=None, help="second pass: check each URL is now a HIT")
    g.add_argument("--verify-delay", type=float, help="seconds to wait between the passes (default 0)")
    g.add_argument("--min-hit-rate", type=float, metavar="PCT", help="exit 1 if the hit rate is below PCT")
    g.add_argument("--max-errors", type=int, metavar="N", help="exit 1 if more than N requests failed")
    g.add_argument("--unknown-as-miss", action="store_true", default=None, help="count unknown status as miss")
    g.add_argument("-f", "--format", choices=FORMATS, help="report format (default table)")
    g.add_argument("-o", "--output", metavar="FILE", help="write the report to FILE instead of standard output")
    g.add_argument("--slowest", type=int, help="number of slowest URLs in the summary (default 10)")
    g.add_argument("--summary-only", action="store_true", help="table format: no per-URL rows")
    g = w.add_argument_group("locking")
    g.add_argument("--lock-file", metavar="PATH", help="refuse to start while another run holds this lock")
    g.add_argument("--lock-wait", type=float, metavar="SECONDS", help="wait up to SECONDS for the lock (default 0)")

    u = sub.add_parser(
        "urls", parents=[shared], help="print the URLs a warm run would request, without requesting them"
    )
    u.add_argument("sitemaps", nargs="*", metavar="SITEMAP")
    _common(u)

    k = sub.add_parser("check", parents=[shared], help="request URLs once and show the detected cache status")
    k.add_argument("urls", nargs="+", metavar="URL")
    _common(k, selection=False)
    _request_options(k)
    k.add_argument("--twice", action="store_true", help="request each URL twice and show both answers")
    k.add_argument("-f", "--format", choices=FORMATS, help="report format (default table)")
    return parser


def _split(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        out.extend(v.strip() for v in value.split(",") if v.strip())
    return out


def _merge(args: argparse.Namespace, config: Config) -> Config:
    """Command-line options override the config file."""
    if getattr(args, "sitemaps", None):
        config.sitemaps = list(args.sitemaps)
    if getattr(args, "urls", None) and args.command != "check":
        config.url_files = [*config.url_files, *args.urls]
    for name in ("include", "exclude"):
        values = getattr(args, name, None)
        if values:
            setattr(config, name, [*getattr(config, name), *values])
    if getattr(args, "profile", None):
        config.profiles = _split(args.profile)
    for raw in getattr(args, "header", None) or []:
        try:
            name, value = parse_header(raw)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        config.headers[name] = value
    if getattr(args, "resolve", None):
        config.resolve = [*config.resolve, *args.resolve]
    for name in (
        "order",
        "limit",
        "since",
        "concurrency",
        "rate",
        "delay",
        "timeout",
        "connect_timeout",
        "retries",
        "follow_redirects",
        "insecure",
        "layer",
        "verify",
        "verify_delay",
        "min_hit_rate",
        "max_errors",
        "unknown_as_miss",
        "format",
        "output",
        "slowest",
        "lock_file",
        "lock_wait",
    ):
        value = getattr(args, name, None)
        if value is not None:
            setattr(config, name, value)
    config.validate()
    return config


def _settings(config: Config) -> WarmSettings:
    return WarmSettings(
        concurrency=config.concurrency,
        rate=config.rate,
        delay=config.delay,
        timeout=config.timeout,
        connect_timeout=config.connect_timeout,
        retries=config.retries,
        follow_redirects=config.follow_redirects,
        layer=config.layer,
        verify=config.verify,
        verify_delay=config.verify_delay,
        extra_headers=dict(config.headers),
        insecure=config.insecure,
    )


def _configure_logging(args: argparse.Namespace) -> None:
    level = logging.ERROR if args.quiet else (logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)]
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stderr, force=True)


def _log_result(result: RequestResult) -> None:
    ttfb = "-" if result.ttfb is None else f"{result.ttfb * 1000:.0f}ms"
    status = result.error or f"{result.status_code} {result.detection.status}"
    log.info("%-6s %-8s %s %s %s", result.phase, result.profile, status, ttfb, result.url)


async def _select_urls(config: Config, client: httpx.AsyncClient) -> tuple[list[str], int]:
    listed: list[SitemapEntry] = []
    for path in config.url_files:
        listed.extend(read_url_list(path))
    entries = list(listed)
    sitemaps_read = 0
    if config.sitemaps:
        read = await read_sitemaps(client, config.sitemaps, max_depth=config.sitemap_depth)
        if not read.entries:
            for source, message in read.errors:
                log.error("sitemap %s: %s", source, message)
            if not listed:
                return [], EXIT_NO_URLS
        entries.extend(read.entries)
        sitemaps_read = len(read.sitemaps_read)
    if not entries:
        log.error("the URL list(s) are empty")
        return [], EXIT_NO_URLS
    selection = select(
        entries,
        include=config.include,
        exclude=config.exclude,
        order=config.order,
        limit=config.limit,
        since=config.since_datetime(),
    )
    log.info(
        "%d sitemap(s), %d listed URL(s), %d entries: %d selected, %d duplicate, %d excluded, %d older than --since, "
        "%d over --limit, %d invalid",
        sitemaps_read,
        len(listed),
        len(entries),
        len(selection.urls),
        selection.duplicates,
        selection.excluded,
        selection.too_old,
        selection.over_limit,
        selection.invalid,
    )
    if not selection.urls:
        log.error("no URL left after filtering")
        return [], EXIT_NO_URLS
    return [e.loc for e in selection.urls], EXIT_OK


def _sitemap_client(transport: httpx.AsyncBaseTransport | None, config: Config) -> httpx.AsyncClient:
    """Client for reading sitemaps; it honours --resolve and --insecure like the warming client."""
    base: httpx.AsyncBaseTransport = transport or httpx.AsyncHTTPTransport(verify=not config.insecure, retries=1)
    rules = config.resolve_rules()
    if rules:
        base = ResolvingTransport(base, rules)
    return httpx.AsyncClient(
        transport=base,
        timeout=httpx.Timeout(config.timeout, connect=config.connect_timeout),
        headers={"User-Agent": TOOL_UA, "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.5"},
        trust_env=False,
    )


def _write(text: str, output: str) -> None:
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


async def _warm(args: argparse.Namespace, config: Config, transport: httpx.AsyncBaseTransport | None) -> int:
    started = time.monotonic()
    async with _sitemap_client(transport, config) as sitemap_client:
        urls, code = await _select_urls(config, sitemap_client)
    if code:
        return code
    profiles = resolve_profiles(config.profiles, config.custom_profiles)
    settings = _settings(config)
    async with make_client(settings, transport, config.resolve_rules()) as client:
        warmer = Warmer(client, settings, profiles, on_result=_log_result)
        results = await warmer.run(urls)
        requests = warmer.requests
    summary = summarize(
        results,
        requests=requests,
        slowest=config.slowest,
        unknown_as_miss=config.unknown_as_miss,
        duration=time.monotonic() - started,
    )
    if config.format == "json":
        report = format_json(results, summary)
    elif config.format == "csv":
        report = format_csv(results)
    else:
        report = format_table(results, summary, show_rows=not args.summary_only)
    if config.output or not args.quiet:
        _write(report, config.output)
    if config.output and config.format != "table" and not args.quiet:
        # Keep a short summary on the terminal when the report goes to a file.
        sys.stderr.write(format_table([], summary, show_rows=False))

    code = EXIT_OK
    if config.max_errors is not None and summary.errors + summary.http_errors > config.max_errors:
        log.error(
            "%d failed request(s), more than --max-errors %d", summary.errors + summary.http_errors, config.max_errors
        )
        code = EXIT_THRESHOLD
    if config.min_hit_rate is not None:
        if summary.hit_rate is None:
            log.error("no cache status detected, so the hit rate cannot reach --min-hit-rate %s", config.min_hit_rate)
            code = EXIT_THRESHOLD
        elif summary.hit_rate < config.min_hit_rate:
            log.error("hit rate %.1f%% is below --min-hit-rate %s", summary.hit_rate, config.min_hit_rate)
            code = EXIT_THRESHOLD
    return code


async def _urls(config: Config, transport: httpx.AsyncBaseTransport | None) -> int:
    async with _sitemap_client(transport, config) as client:
        urls, code = await _select_urls(config, client)
    if code:
        return code
    sys.stdout.write("\n".join(urls) + "\n")
    return EXIT_OK


async def _check(args: argparse.Namespace, config: Config, transport: httpx.AsyncBaseTransport | None) -> int:
    config.verify = bool(args.twice)
    profiles = resolve_profiles(config.profiles, config.custom_profiles)
    settings = _settings(config)
    started = time.monotonic()
    async with make_client(settings, transport, config.resolve_rules()) as client:
        warmer = Warmer(client, settings, profiles, on_result=_log_result)
        results = await warmer.run(list(args.urls))
    summary = summarize(results, requests=warmer.requests, slowest=0, duration=time.monotonic() - started)
    if config.format == "json":
        sys.stdout.write(format_json(results, summary))
    elif config.format == "csv":
        sys.stdout.write(format_csv(results))
    else:
        sys.stdout.write(format_table(results, summary))
    return EXIT_OK if all(not r.final.error for r in results) else EXIT_THRESHOLD


def main(argv: list[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args)
    try:
        config = load_config(args.config) if args.config else Config()
        config = _merge(args, config)
        if args.command in ("warm", "urls") and not (config.sitemaps or config.url_files):
            log.error("give at least one sitemap URL or file, or --urls FILE (or set warm.sitemaps in the config)")
            return EXIT_USAGE
        if args.command == "check":
            resolve_profiles(config.profiles, config.custom_profiles)
            return asyncio.run(_check(args, config, transport))
        if args.command == "urls":
            return asyncio.run(_urls(config, transport))
        resolve_profiles(config.profiles, config.custom_profiles)
        lock = LockFile(config.lock_file, wait=config.lock_wait) if config.lock_file else None
        with lock if lock is not None else contextlib.nullcontext():
            return asyncio.run(_warm(args, config, transport))
    except LockError as exc:
        log.warning("another run is active: %s", exc)
        return EXIT_LOCKED
    except (ConfigError, ValueError) as exc:
        log.error("%s", exc)
        return EXIT_USAGE
    except OSError as exc:
        log.error("%s", exc)
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `warm --urls FILE` and `urls --urls FILE` (repeatable, `-` reads standard input; config key `url_files`): warm a
  plain list of URLs, on its own or together with sitemaps. Lines are one URL each, with `#` comments and extra
  fields after the URL ignored. Listed URLs come before sitemap URLs, are de-duplicated and filtered like them, and
  are kept by `--since` because they have no lastmod. Library: `parse_url_list()`, `read_url_list()` and
  `SitemapEntry.listed`.
- The selection log line also counts listed and invalid URLs.
- A code of conduct, issue forms and a pull request template.

## [0.1.0] - 2026-11-16

### Added

- Sitemap reader for sitemap indexes and urlsets, plain or gzip, remote or local, with nesting, size and entity
  limits.
- URL selection: de-duplication, include/exclude regular expressions, ordering by sitemap, priority, lastmod,
  priority then lastmod or URL, `--limit` and `--since` (date or age).
- Async warming with a concurrency limit, per-host rate limit and delay, connect and total timeouts, retries with
  exponential backoff and `Retry-After`; cookies are never stored.
- Header profiles `desktop`, `mobile` and `tool`, custom profiles in the config and extra headers with `-H`.
- `--resolve host:port:address` to warm the origin server directly while keeping the `Host` header, SNI and
  certificate verification.
- Cache status detection for Cloudflare, `x-cache` CDNs, Varnish, LiteSpeed, Nginx FastCGI/proxy caches,
  WordPress plugin headers and the footer comments of Cache Enabler, WP Super Cache, W3 Total Cache,
  WP Fastest Cache, WP Rocket, Hummingbird and Breeze; `--layer` to judge one layer.
- Two-pass mode with plugin timestamp comparison, hit rate, TTFB statistics and the slowest URLs.
- Table, JSON and CSV reports; `--min-hit-rate` and `--max-errors` exit codes.
- `flock`-based lock file for cron and systemd runs, with example crontab, systemd service and timer.
- `urls` and `check` commands, TOML configuration, Docker image published to
  `ghcr.io/saizul/sitemap-cache-warmer`.

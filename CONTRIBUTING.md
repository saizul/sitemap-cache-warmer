# Contributing

Thanks for taking the time to improve sitemap-cache-warmer. Bug reports, new
cache header formats, plugin footer comments and documentation fixes are all
welcome.

## Reporting a bug

Open an issue with:

- the command you ran and its output (`-vv` adds debug logging)
- your Python version and operating system
- for wrong cache detection: the output of
  `sitemap-cache-warmer check URL --twice -f json`, or the response headers
  (`curl -sI URL`) and the last lines of the HTML, with your domain replaced by
  `example.com`
- the cache stack: CDN, web server, cache plugin and their versions

Never paste cookies, access tokens or private staging URLs.

## Development setup

You need Python 3.10 or newer. No network access is needed for development.

```console
$ git clone https://github.com/saizul/sitemap-cache-warmer.git
$ cd sitemap-cache-warmer
$ python -m venv .venv
$ . .venv/bin/activate
$ python -m pip install -e ".[dev]"
```

Before every commit:

```console
$ ruff check .
$ ruff format --check .
$ pytest
```

All three must pass; CI runs them on Python 3.10 to 3.13.

## Guidelines

- **Tests never use the network.** Extend the fake cache in
  `tests/fakecache.py` (served through `httpx.MockTransport`) or add canned
  responses with `overrides`.
- **New detection rules need evidence.** Link the documentation of the cache or
  plugin, or paste a real (anonymised) response in the pull request, and add a
  test with that exact header or comment. Say whether the marker also appears
  on the response that fills the cache: if it does, the status must stay
  `unknown` and use a timestamp fingerprint instead of `hit`.
- **Be gentle by default.** Defaults must stay safe for shared hosting: low
  concurrency, retries with backoff, no cookies.
- **The JSON report and the exit codes are a contract.** Do not rename keys or
  change codes without a changelog entry.
- **Every change comes with a test** that fails without it.
- Add a line to the `Unreleased` section of `CHANGELOG.md` for anything a user
  would notice.

## Project layout

```text
src/sitemap_cache_warmer/
    sitemap.py       sitemap and sitemap index reading, lastmod parsing
    selection.py     de-duplication, filters, ordering, limit
    profiles.py      browser-like header profiles
    cache_status.py  header and footer comment detection
    ratelimit.py     per-host pacing
    resolve.py       --resolve transport (Host header and SNI kept)
    warmer.py        the async engine: retries, TTFB, two passes
    report.py        summary, table, JSON and CSV
    lock.py          flock-based lock file
    config.py        TOML settings
    cli.py           command-line interface
tests/               pytest suite and the fake cache
examples/            config, cron, systemd, deploy script, sitemap, library use
```

## Pull requests

- Keep each pull request focused on one change.
- Describe what changed and why, and how you tested it.
- Do not bump the version number; that happens at release time.

By contributing you agree that your work is released under the MIT License.

# sitemap-cache-warmer

[![CI](https://github.com/saizul/sitemap-cache-warmer/actions/workflows/ci.yml/badge.svg)](https://github.com/saizul/sitemap-cache-warmer/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

Fill your page caches before visitors arrive, and prove it worked. `sitemap-cache-warmer` reads your sitemaps,
requests every page the way a real desktop or mobile browser would, and reads the cache status from the response:
Cloudflare and other CDNs, Varnish, Nginx FastCGI and proxy caches, LiteSpeed, and the footer comments of common
WordPress cache plugins. In two-pass mode it requests everything again and checks that the second answer is a
**HIT**. You get the hit rate, TTFB figures and the slowest URLs, and a non-zero exit code when the hit rate is too
low.

A purged cache means the next visitor to each page waits for PHP and the database. After a deploy, a plugin update
or a nightly purge, that can be seconds per page for every page nobody has opened yet. Search engine crawlers hit
those cold pages too. A plain `wget --spider` loop doesn't solve this: it sends the wrong `User-Agent` and
`Accept-Encoding`, so the cache stores a variant that real browsers never request. It also can't tell you whether
anything was cached.

```console
$ sitemap-cache-warmer warm https://www.example.com/sitemap_index.xml --profile desktop,mobile --verify
URL                                       PROFILE  CODE WARM       TTFB VERIFY     TTFB  SOURCE
https://www.example.com/                  desktop   200 miss        812 hit          38  cf-cache-status: HIT
https://www.example.com/                  mobile    200 miss        790 hit          41  cf-cache-status: HIT
https://www.example.com/shop/             desktop   200 miss       1430 hit          45  cf-cache-status: HIT
https://www.example.com/cart/             desktop   200 bypass      260 bypass      255  cf-cache-status: DYNAMIC
…

148 URL(s), 296 result(s), 590 request(s) in 94.2s
cache (verify pass): 286 hit, 4 miss, 6 bypass, 0 unknown, 0 error
hit rate: 96.6%
TTFB warm: median 702 ms, p95 1650 ms, max 2210 ms
TTFB verify: median 44 ms, p95 120 ms, max 410 ms
slowest:
     410 ms  miss    mobile   https://www.example.com/blog/huge-gallery/
…
```

## Contents

- [Features](#features)
- [Installation](#installation)
- [Docker image](#docker-image)
- [Usage](#usage)
- [How cache status is detected](#how-cache-status-is-detected)
- [Header profiles](#header-profiles)
- [Warming from the origin server](#warming-from-the-origin-server)
- [Configuration](#configuration)
- [Reports](#reports)
- [Exit codes](#exit-codes)
- [Scheduling: cron and systemd](#scheduling-cron-and-systemd)
- [Library use](#library-use)
- [Examples](#examples)
- [Testing](#testing)
- [Security notes](#security-notes)
- [Limitations](#limitations)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Sitemaps**: `<sitemapindex>` and `<urlset>`, nested indexes, gzip (`.xml.gz`), remote URLs or local files,
  with size limits and entity-expansion protection.
- **URL lists**: `--urls FILE` (or `-` for standard input) warms a plain list of URLs, alone or together with
  sitemaps. Listed URLs come first, go through the same filters, and are kept by `--since`.
- **Selection**: de-duplication across sitemaps, include and exclude regular expressions, order by sitemap,
  priority, lastmod, priority then lastmod, or URL, a `--limit`, and `--since 7d` / `--since 2026-11-01` to warm
  only recently changed pages.
- **Polite, fast warming**: async `httpx` with a global concurrency limit, a per-host rate limit (`--rate`
  requests per second) and delay, connect and total timeouts, and retries with exponential backoff on network
  errors, 429, 502, 503 and 504, honouring `Retry-After`.
- **Header profiles**: `desktop` and `mobile` browser profiles (`User-Agent`, `Accept: text/html…`,
  `Accept-Encoding: gzip, deflate, br`, `Accept-Language`), an honest `tool` profile, custom profiles in the
  config, and extra headers with `-H`. Cookies are never stored or sent, so a session cookie from one page can't
  turn the rest of the run into cache bypasses.
- **`--resolve host:port:address`** works like curl's option: connect to a chosen IP (the origin server, a
  specific edge node or `127.0.0.1`) while keeping the real `Host` header, TLS server name and certificate check.
- **Cache detection**: `cf-cache-status`, `x-cache` (CloudFront, Fastly, Akamai, KeyCDN, Varnish), `cdn-cache`,
  `x-vercel-cache`, `x-varnish` with `age`, `x-litespeed-cache`, `x-fastcgi-cache`, `x-proxy-cache`,
  `x-cache-status`, plugin headers, and footer comments of Cache Enabler, WP Super Cache, W3 Total Cache,
  WP Fastest Cache, WP Rocket, Hummingbird and Breeze. Every result is one of `hit`, `miss`, `bypass` or
  `unknown`, together with the header or comment it came from.
- **Two-pass mode** (`--verify`): warm, wait (`--verify-delay`), request again, and judge the second answer.
  For plugins whose comment looks the same on a miss and a hit, the plugin's cache timestamp is compared between
  the passes.
- **Reports**: a text table, JSON or CSV, with per-URL status and TTFB for both passes, a summary with counts per
  profile, hit rate, median, p95 and max TTFB, and the slowest URLs.
- **CI and cron friendly**: `--min-hit-rate 90` and `--max-errors N` set the exit code, and `--lock-file` keeps
  overlapping runs from stacking up. Example cron and systemd timer files are included.

## Installation

You need Python 3.10 or newer. The runtime dependency is `httpx`. Install the `brotli` extra too, so footer
comments can be read from Brotli-compressed responses.

```console
$ pip install "sitemap-cache-warmer[brotli]"     # from PyPI, once published
```

From a checkout:

```console
$ git clone https://github.com/saizul/sitemap-cache-warmer.git
$ cd sitemap-cache-warmer
$ python -m pip install ".[brotli]"
```

On a server, an isolated install with [pipx](https://pipx.pypa.io/) keeps it apart from system Python packages:
`pipx install "sitemap-cache-warmer[brotli]"`.

## Docker image

Every push to `main` and every release publishes a multi-arch image (`linux/amd64`, `linux/arm64`) to the GitHub
Container Registry:

```console
$ docker pull ghcr.io/saizul/sitemap-cache-warmer:latest
$ docker run --rm ghcr.io/saizul/sitemap-cache-warmer warm https://www.example.com/sitemap.xml --verify
```

The working directory inside the container is `/work`. Mount a folder there for a config file, a local sitemap
or the report, and run as your own user so the files belong to you:

```console
$ docker run --rm -v "$PWD:/work" --user "$(id -u):$(id -g)" ghcr.io/saizul/sitemap-cache-warmer \
    warm -c warmer.toml -f json -o reports/warm.json
```

To warm the origin from the same host, use `--network host` and `--resolve www.example.com:443:127.0.0.1`.

Tags: `latest` (main branch), the short commit SHA, and the version number for releases (`0.1.0`). The image runs
as an unprivileged user (UID 10001) and includes Brotli support.

## Usage

```text
sitemap-cache-warmer warm  [SITEMAP ...] [--urls FILE] [-c FILE] [selection] [requests] [--verify] [output] [--lock-file PATH]
sitemap-cache-warmer urls  [SITEMAP ...] [--urls FILE] [-c FILE] [selection]
sitemap-cache-warmer check URL ... [--twice] [requests]
```

`-v` logs every request (`-vv` for debug details), `-q` prints only errors. Both work before or after the command.

```console
# Warm everything once, desktop profile
$ sitemap-cache-warmer warm https://www.example.com/sitemap_index.xml

# Desktop and mobile copies, two passes, fail below 90% hits
$ sitemap-cache-warmer warm https://www.example.com/sitemap_index.xml --profile desktop,mobile --verify --min-hit-rate 90

# Only pages changed in the last day, newest first, 2 requests per second
$ sitemap-cache-warmer warm https://www.example.com/sitemap.xml --since 1d --order lastmod --rate 2

# Skip the shop flow and query strings, warm the 200 most important pages
$ sitemap-cache-warmer warm https://www.example.com/sitemap.xml --exclude '/(cart|checkout|my-account)/' \
    --exclude '\?' --order priority --limit 200

# Warm a list of URLs (changed pages from a deploy script), on its own or before the sitemap
$ sitemap-cache-warmer warm --urls changed-urls.txt --verify
$ printf '%s\n' https://www.example.com/ https://www.example.com/pricing/ | sitemap-cache-warmer warm --urls -
$ sitemap-cache-warmer warm https://www.example.com/sitemap.xml --urls priority-pages.txt --since 1d

# What would be warmed? (no page requests)
$ sitemap-cache-warmer urls https://www.example.com/sitemap.xml --order priority --limit 20

# Look at one URL: is it cached, and by which layer?
$ sitemap-cache-warmer check https://www.example.com/ --twice -v
```

| Option | Meaning |
| --- | --- |
| `--urls FILE` | one URL per line (`#` comments, extra fields after the URL ignored); `-` reads standard input; repeatable |
| `--include RE`, `--exclude RE` | regular expressions searched in the full URL (repeatable) |
| `--order` | `sitemap` (default), `priority`, `lastmod`, `priority-lastmod`, `url` |
| `--limit N` | warm at most N URLs (after ordering) |
| `--since X` | only URLs with a `lastmod` after a date (`2026-11-01`) or within an age (`36h`, `7d`, `2w`) |
| `--profile NAME` | `desktop`, `mobile`, `tool` or a config profile; repeatable or comma separated |
| `-H 'Name: value'` | extra header for every request (overrides the profile) |
| `--resolve HOST:PORT:ADDR` | connect to `ADDR` for `HOST:PORT` (repeatable) |
| `--concurrency N` | parallel requests (default 4) |
| `--rate R`, `--delay S` | per-host pacing: at most R requests per second, at least S seconds apart |
| `--timeout S`, `--connect-timeout S` | default 30 and 10 seconds |
| `--retries N` | default 2, with backoff of 1 s, 2 s, 4 s… or the server's `Retry-After` (capped at 60 s) |
| `--follow-redirects` | follow redirects (by default a redirect is reported, not followed) |
| `--insecure` | skip TLS certificate checks (see [security notes](#security-notes)) |
| `--layer L` | judge only one layer: `cdn`, `varnish`, `litespeed`, `nginx`, `plugin` or `age` |
| `--verify`, `--verify-delay S` | second pass, after S seconds |
| `--min-hit-rate PCT` | exit 1 if the hit rate is below PCT |
| `--max-errors N` | exit 1 if more than N requests failed (network errors and HTTP 4xx/5xx) |
| `--unknown-as-miss` | count an undetectable status as a miss in the hit rate |
| `-f table\|json\|csv`, `-o FILE` | report format and destination |
| `--slowest N`, `--summary-only` | size of the slowest list; table without per-URL rows |
| `--lock-file PATH`, `--lock-wait S` | refuse to start while another run holds the lock, or wait up to S seconds |

## How cache status is detected

Each layer that leaves a trace in the response becomes a signal. The overall status comes from the layer closest
to the visitor: CDN, then Varnish, LiteSpeed, Nginx, then WordPress plugins, and `Age` as a last resort. Use
`--layer nginx` (for example) to judge one layer only, which is useful when the CDN sits in front but you want to
know whether the origin cache is warm.

| Source | Values | Status |
| --- | --- | --- |
| `cf-cache-status` (Cloudflare) | `HIT`, `STALE`, `UPDATING`, `REVALIDATED` | hit |
| | `MISS`, `EXPIRED` | miss |
| | `BYPASS`, `DYNAMIC` | bypass |
| `x-cache` (CloudFront, Fastly, Akamai, KeyCDN, Varnish setups) | contains `HIT` (`Hit from cloudfront`, `TCP_MEM_HIT`, `MISS, HIT`) | hit |
| | `MISS`, `EXPIRED`, `TCP_MISS`, `TCP_REFRESH_MISS` | miss |
| | `PASS`, `BYPASS` | bypass |
| `cdn-cache`, `x-vercel-cache`, `x-sucuri-cache`, `x-qc-cache` | as `x-cache` | |
| `x-varnish` | two request IDs (`32770 32768`) | hit |
| | one request ID | miss |
| `x-litespeed-cache` | `hit`, `hit,private` / `miss` | hit / miss |
| `x-litespeed-cache-control: no-cache` | | bypass |
| `x-fastcgi-cache`, `x-proxy-cache`, `x-nginx-cache`, `x-cache-status`, `x-srcache-fetch-status` | Nginx `$upstream_cache_status` values, as `cf-cache-status` | |
| `WP-Super-Cache: Served …` header | | hit |
| `X-Cache-Handler: cache-enabler…` header | | hit |
| `x-rocket-nginx-bypass: Yes` / `No` | | bypass / hit |
| `<!-- Cache Enabler by KeyCDN @ … -->` | only in the cached copy | hit |
| `<!-- WP Fastest Cache file was created in … -->` | | hit |
| `<!-- Debug: cached@… -->` (WP Rocket) | | hit |
| `<!-- Hummingbird cache file was created in … -->` | | hit |
| `<!-- Cache served by breeze CACHE … -->` | | hit |
| WP Super Cache "Cached page" comment | the same on the response that created the file | unknown, then timestamp comparison |
| `<!-- super cache -->` (WP Super Cache static file) | | hit |
| `<!-- Dynamic page generated in … -->` alone | rendered by PHP, not cached | miss |
| W3 Total Cache "Served from: … @ time" footer | | unknown, then timestamp comparison |
| W3 Total Cache "Page Caching using … (reason)" | a reason means the page was not cached | bypass |
| `Age` greater than 0 | some cache served it | hit |

Footer comments are read from the last 64 KB of HTML responses only. **Timestamp comparison**: in two-pass mode,
when the verify response still has an `unknown` status but carries the same plugin cache timestamp as the warm
response, it was served from the cache file the first request created (`hit`). A different timestamp means the
page was rendered again (`miss`).

## Header profiles

Many page caches store more than one copy of each page:

- **Device**: WP Rocket, LiteSpeed Cache and others can keep a separate mobile cache, chosen by the
  `User-Agent`. Warming only with a desktop browser leaves phones on a cold cache.
- **Compression**: Nginx and CDNs vary on `Accept-Encoding`. A client that does not send `br` or `gzip` can get
  its own uncompressed variant, which real browsers never ask for.
- **Content type**: some setups only cache requests that accept `text/html`.

The built-in profiles send what current Chrome sends on desktop and on Android: `User-Agent`, `Accept`,
`Accept-Encoding: gzip, deflate, br`, `Accept-Language`, `Sec-CH-UA-Mobile` and `Upgrade-Insecure-Requests`.
`tool` identifies itself as `sitemap-cache-warmer/0.1.0`. Use it when your cache doesn't vary on the
`User-Agent` and you want the requests to be easy to spot in the logs.

Custom profiles go in the config file:

```toml
[profiles.tablet.headers]
User-Agent = "Mozilla/5.0 (iPad; CPU OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1"
Accept = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
Accept-Encoding = "gzip, deflate, br"
```

Cookies are dropped from every response, so the client never sends any back. Logged-in, cart and consent cookies
make most WordPress caches bypass the page, and that is not the copy you want to warm. If your cache varies on a
cookie on purpose, for example a currency or language cookie, send it with `-H 'Cookie: currency=EUR'`.

## Warming from the origin server

With a CDN in front, warming through the public host name fills the CDN edge node that serves *you*. Each edge
location has its own cache. Running the warmer on the origin server and connecting to it directly warms the origin
cache (Nginx FastCGI, Varnish, LiteSpeed, plugin files), which every edge then pulls from:

```console
$ sitemap-cache-warmer warm https://www.example.com/sitemap.xml --resolve www.example.com:443:127.0.0.1 \
    --layer nginx --verify
```

The connection goes to `127.0.0.1`, but the `Host` header and the TLS server name (SNI) stay `www.example.com`.
The web server picks the right virtual host, and the certificate is still checked against `www.example.com`. If
the origin uses a certificate that public clients don't trust (for example a CDN origin certificate), add
`--insecure` for this run only. The sitemap is fetched through the same `--resolve` mapping.

## Configuration

All options can be set in a TOML file under `[warm]`. Options on the command line override the file.
[`examples/warmer.toml`](examples/warmer.toml) lists every key:

```toml
[warm]
sitemaps = ["https://www.example.com/sitemap_index.xml"]
url_files = ["priority-pages.txt"]   # optional URL lists, warmed first
exclude = ['/cart/', '/checkout/', '/my-account/', '\?']
order = "priority-lastmod"
profiles = ["desktop", "mobile"]
headers = { "X-Cache-Warmer" = "1" }
concurrency = 4
rate = 5.0
verify = true
verify_delay = 2.0
min_hit_rate = 90.0
lock_file = "~/.cache/sitemap-cache-warmer.lock"
format = "json"
output = "reports/warm.json"
```

Unknown keys, wrong types, bad regular expressions, bad `resolve` entries and invalid header names are errors
(exit code 2), so a typo never silently changes a scheduled run. There are no secrets in the config. If a staging
site needs a header such as an access token, set it from an environment variable in your scheduler:
`-H "X-Staging-Access: $STAGING_ACCESS_TOKEN"`.

## Reports

- **table** (default): one row per URL and profile with the warm and verify status and TTFB in milliseconds, and
  the header or comment the status came from. Then the summary. `--summary-only` drops the rows.
- **json**: `{"summary": {...}, "results": [...]}`. Each result has both passes with status code, TTFB, total
  time, body size, attempts, redirect location and every cache signal found.
- **csv**: one row per URL and profile. Cells that a spreadsheet would run as a formula are prefixed with `'`.

When `-o` writes JSON or CSV to a file, a short summary still goes to standard error unless `-q` is given.

The **hit rate** is computed from the last pass (the verify pass in two-pass mode), over 2xx responses whose
status is `hit`, `miss` or `bypass`. Pages with an `unknown` status are left out unless you pass
`--unknown-as-miss`. Redirects, 4xx and 5xx responses and network errors are counted separately.

**TTFB** is the time from sending the request until the response headers arrive, including DNS, TCP and TLS on a
new connection. Connections are reused, so later requests to the same host are usually faster.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | hit rate below `--min-hit-rate`, more failures than `--max-errors`, or (for `check`) a request failed |
| 2 | bad arguments or configuration |
| 3 | no sitemap could be read, or no URL left after filtering |
| 4 | another run holds the lock file |

## Scheduling: cron and systemd

Use a lock file for every scheduled run. If a run is still busy when the next one starts (a slow site, a big
sitemap), the new run exits at once with code 4 instead of doubling the load. On Linux and macOS the lock is a
kernel `flock`. It is released when the process exits, even after a crash or `kill -9`, so a leftover file never
blocks the next run. Put the lock in a directory only your user can write, not in `/tmp`.

### cron

```cron
# m   h  dom mon dow  command
*/30  *  *   *   *    sitemap-cache-warmer warm https://www.example.com/sitemap_index.xml --profile desktop,mobile --verify --rate 5 --lock-file "$HOME/.cache/sitemap-cache-warmer.lock" --summary-only -q -f json -o "$HOME/.cache/sitemap-cache-warmer/last-run.json" >> "$HOME/.cache/sitemap-cache-warmer.log" 2>&1
```

Run it as an unprivileged user. A copy with comments is in [`examples/crontab.txt`](examples/crontab.txt).

### systemd timer

[`examples/sitemap-cache-warmer.service`](examples/sitemap-cache-warmer.service):

```ini
[Unit]
Description=Warm the page cache from the sitemap
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=cachewarmer
Group=cachewarmer
StateDirectory=sitemap-cache-warmer
ExecStart=/usr/local/bin/sitemap-cache-warmer warm -c /etc/sitemap-cache-warmer.toml \
    --lock-file /var/lib/sitemap-cache-warmer/run.lock \
    -f json -o /var/lib/sitemap-cache-warmer/last-run.json
SuccessExitStatus=4
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=2h
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
```

[`examples/sitemap-cache-warmer.timer`](examples/sitemap-cache-warmer.timer):

```ini
[Unit]
Description=Warm the page cache every 30 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=30min
RandomizedDelaySec=2min
Persistent=true

[Install]
WantedBy=timers.target
```

```console
$ sudo useradd --system --no-create-home cachewarmer
$ sudo cp examples/sitemap-cache-warmer.service examples/sitemap-cache-warmer.timer /etc/systemd/system/
$ sudo systemctl daemon-reload
$ sudo systemctl enable --now sitemap-cache-warmer.timer
$ systemctl list-timers sitemap-cache-warmer.timer
$ journalctl -u sitemap-cache-warmer.service -n 20
```

`SuccessExitStatus=4` treats "previous run still busy" as success. A hit rate below `min_hit_rate` (exit code 1)
marks the unit as failed, which your monitoring can alert on (`systemctl --failed`, `OnFailure=`).

### After a deploy or a purge

[`examples/after-deploy.sh`](examples/after-deploy.sh) first warms the pages changed in the last day, then
everything, with a verify pass and a minimum hit rate, so a deploy pipeline can fail when caching broke.
If your deploy knows exactly which pages changed, write them to a file (or pipe them) and pass `--urls FILE`:
those URLs are warmed without reading any sitemap.

## Library use

```python
import asyncio
from sitemap_cache_warmer import BUILTIN_PROFILES, Warmer, WarmSettings, make_client, summarize

async def main() -> None:
    settings = WarmSettings(concurrency=4, rate=5, verify=True)
    async with make_client(settings) as client:
        warmer = Warmer(client, settings, [BUILTIN_PROFILES["desktop"], BUILTIN_PROFILES["mobile"]])
        results = await warmer.run(["https://www.example.com/", "https://www.example.com/shop/"])
    summary = summarize(results, requests=warmer.requests)
    print(summary.hit_rate, [r.cache_status for r in results])

asyncio.run(main())
```

`detect(headers, body)` can be used on its own to classify any response. `make_client` accepts an `httpx`
transport, so [`examples/library_usage.py`](examples/library_usage.py) runs offline against `httpx.MockTransport`.

## Examples

- [`examples/warmer.toml`](examples/warmer.toml): every option, with a custom tablet profile.
- [`examples/crontab.txt`](examples/crontab.txt), [`examples/sitemap-cache-warmer.service`](examples/sitemap-cache-warmer.service)
  and [`examples/sitemap-cache-warmer.timer`](examples/sitemap-cache-warmer.timer): scheduling.
- [`examples/after-deploy.sh`](examples/after-deploy.sh): warm and verify in a deploy pipeline.
- [`examples/sitemap.xml`](examples/sitemap.xml): a small sitemap for trying `urls` offline.
- [`examples/library_usage.py`](examples/library_usage.py): the library API against a fake cache.

## Testing

```console
$ python -m pip install -e ".[dev]"
$ pytest
$ ruff check . && ruff format --check .
```

The tests never use the network. [`tests/fakecache.py`](tests/fakecache.py) is a fake site behind a cache, served
through `httpx.MockTransport`. It stores one copy per path, device and encoding and answers in Nginx,
Cloudflare, Varnish or LiteSpeed style, with optional plugin footers. The tests cover every header and footer
rule, sitemap parsing (gzip, limits, entities), selection and ordering, profiles and variants, cookies,
`--resolve` (host header and SNI), retries and `Retry-After`, the rate limiter, the lock across processes, the
reports and every exit code. CI runs them on Python 3.10 to 3.13. It also smoke-tests the CLI offline, including
the lock exit code with `flock(1)`, and builds the Docker image.

## Security notes

- **Only warm sites you run or are allowed to load-test.** Warming is many uncached requests in a short time,
  which is exactly the load a cache exists to absorb. Start with low `--concurrency` and a `--rate`, especially on
  shared hosting, and raise them while you watch the server.
- **`--insecure`** turns off certificate checks, so anyone on the network path could answer instead of your
  server. Use it only together with `--resolve` to a host you control (an origin with a CDN-only certificate, or
  `127.0.0.1`), never for warming over the internet.
- **No credentials** are stored or needed. Cookies are discarded. Headers passed with `-H` appear in process
  lists and shell history. Pass secret values through environment variables, and don't put them in config files
  you commit.
- **Untrusted input**: sitemaps with a `DOCTYPE` or entities are refused, decompressed sitemaps are capped at 50 MB,
  and page bodies are streamed with only the last 64 KB kept for comment detection. CSV cells are protected
  against formula injection.
- **Lock files** are opened with `O_NOFOLLOW`, so a symlink planted in a shared directory can't redirect the
  write. Still, keep them out of world-writable directories.

See [SECURITY.md](SECURITY.md) for reporting a vulnerability.

## Limitations

- Each CDN edge location has its own cache. Warming through the CDN fills the edge nearest to the machine running
  the warmer, not every edge worldwide. Warm the origin (see above) and let your CDN's tiered cache or origin
  shield do the rest.
- A `Vary: Cookie` or per-currency or per-language cache needs one run (or profile) per variant.
- Detection relies on headers and comments that you can switch off or rename (for example a custom
  `add_header X-Cache-Status` name in Nginx, or a plugin's "remove footer comment" option). Such a site shows
  `unknown`. Add a header in your server config, or use `--layer`.
- Varnish shows a `pass` like a miss (one request ID, `Age: 0`) unless your VCL adds its own header.
- The plugin comment formats are based on the plugins' documented and commonly seen output. Plugins change them
  between versions, so check `check --twice -v` on your own site. W3 Total Cache and WP Super Cache need two
  passes, because their comment is the same on the response that fills the cache.
- No JavaScript is run and no assets (CSS, images, fonts) are requested. Only the HTML documents in the sitemap
  are warmed.
- HTTP/1.1 only. Cache keys rarely depend on the protocol version.

## Contributing

Bug reports and pull requests are welcome, especially header or comment formats from caches that aren't covered
yet. See [CONTRIBUTING.md](CONTRIBUTING.md), the [code of conduct](CODE_OF_CONDUCT.md), and
[CHANGELOG.md](CHANGELOG.md) for the release history.

## License

[MIT](LICENSE) © 2026 Saizul Amin

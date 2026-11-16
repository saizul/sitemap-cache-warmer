# Security policy

## Supported versions

Security fixes are made for the latest release. Please upgrade before reporting.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | yes       |

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private
vulnerability reporting instead: open the **Security** tab of
https://github.com/saizul/sitemap-cache-warmer and choose **Report a
vulnerability**.

Include what you found, how to reproduce it and what an attacker could do with
it. You should get an answer within a week. Once a fix is released you will be
credited in the changelog unless you prefer otherwise.

Do not include cookies, tokens or private URLs in a report. Replace domains
with `example.com`.

## Scope

In scope:

- parsing of untrusted sitemaps and responses: crashes, entity expansion,
  decompression bombs, excessive memory or CPU use
- TLS handling, including `--resolve` (Host header, SNI and certificate checks)
- cookies or headers leaking between requests or hosts
- the lock file (symlink attacks, races that let two runs overlap)
- injection into reports, for example spreadsheet formulas in CSV files
- ignoring the concurrency, rate or delay limits

Out of scope: the behaviour of the caches and plugins being warmed, and load
caused by settings the user chose.

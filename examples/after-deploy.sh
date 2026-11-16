#!/bin/sh
# Purge-and-warm step for a deploy script: warm the pages changed in the last day first, then everything.
# Fails the deploy when fewer than 90% of the pages are cache hits after warming.
set -eu

SITEMAP="${SITEMAP:-https://www.example.com/sitemap_index.xml}"

sitemap-cache-warmer warm "$SITEMAP" --since 1d --order lastmod --profile desktop,mobile -q
sitemap-cache-warmer warm "$SITEMAP" --order priority-lastmod --profile desktop,mobile \
    --verify --verify-delay 2 --min-hit-rate 90 --summary-only

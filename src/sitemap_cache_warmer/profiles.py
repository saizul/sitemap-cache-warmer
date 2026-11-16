"""Request header profiles.

Page caches usually store one copy per variant: WP Rocket and LiteSpeed keep separate mobile copies, Cloudflare and
Nginx vary on ``Accept-Encoding``, and some setups vary on cookies. A warm-up request only helps if it looks like the
requests real visitors send, so the profiles mimic current desktop and mobile browsers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sitemap_cache_warmer._version import __version__

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Mobile Safari/537.36"
)
TOOL_UA = f"sitemap-cache-warmer/{__version__} (+https://github.com/saizul/sitemap-cache-warmer)"

BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"


@dataclass(frozen=True)
class Profile:
    name: str
    headers: dict[str, str] = field(default_factory=dict)

    def merged(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Profile headers with ``extra`` on top (header names compared case-insensitively)."""
        out = dict(self.headers)
        for key, value in (extra or {}).items():
            for existing in [k for k in out if k.lower() == key.lower()]:
                del out[existing]
            out[key] = value
        return out


def _browser(ua: str, mobile: bool) -> dict[str, str]:
    return {
        "User-Agent": ua,
        "Accept": BROWSER_ACCEPT,
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-CH-UA-Mobile": "?1" if mobile else "?0",
        "Upgrade-Insecure-Requests": "1",
    }


BUILTIN_PROFILES: dict[str, Profile] = {
    "desktop": Profile("desktop", _browser(DESKTOP_UA, mobile=False)),
    "mobile": Profile("mobile", _browser(MOBILE_UA, mobile=True)),
    # Identifies itself honestly; useful when the cache does not vary on the User-Agent.
    "tool": Profile("tool", {"User-Agent": TOOL_UA, "Accept": "text/html", "Accept-Encoding": "gzip, deflate, br"}),
}


def parse_header(text: str) -> tuple[str, str]:
    """``"Name: value"`` -> ``("Name", "value")``."""
    name, sep, value = text.partition(":")
    name = name.strip()
    if not sep or not name or any(c in name for c in " \t\r\n"):
        raise ValueError(f"header must look like 'Name: value', got {text!r}")
    if "\n" in value or "\r" in value:
        raise ValueError("header values cannot contain line breaks")
    return name, value.strip()


def resolve_profiles(names: list[str], custom: dict[str, Profile] | None = None) -> list[Profile]:
    available = {**BUILTIN_PROFILES, **(custom or {})}
    out = []
    for name in names:
        if name not in available:
            raise ValueError(f"unknown profile {name!r}; available: {', '.join(sorted(available))}")
        if available[name] not in out:
            out.append(available[name])
    return out

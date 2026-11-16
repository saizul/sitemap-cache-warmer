"""Settings from a TOML file, merged with command-line options."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib

from sitemap_cache_warmer.cache_status import LAYERS
from sitemap_cache_warmer.profiles import Profile, parse_header
from sitemap_cache_warmer.resolve import ResolveRule
from sitemap_cache_warmer.selection import ORDERS

FORMATS = ("table", "json", "csv")


class ConfigError(ValueError):
    """The configuration is invalid."""


@dataclass
class Config:
    sitemaps: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    order: str = "sitemap"
    limit: int = 0
    since: str = ""
    profiles: list[str] = field(default_factory=lambda: ["desktop"])
    headers: dict[str, str] = field(default_factory=dict)
    resolve: list[str] = field(default_factory=list)
    concurrency: int = 4
    rate: float = 0.0
    delay: float = 0.0
    timeout: float = 30.0
    connect_timeout: float = 10.0
    retries: int = 2
    follow_redirects: bool = False
    insecure: bool = False
    layer: str = "auto"
    verify: bool = False
    verify_delay: float = 0.0
    min_hit_rate: float | None = None
    max_errors: int | None = None
    unknown_as_miss: bool = False
    lock_file: str = ""
    lock_wait: float = 0.0
    format: str = "table"
    output: str = ""
    slowest: int = 10
    sitemap_depth: int = 3
    custom_profiles: dict[str, Profile] = field(default_factory=dict)

    def validate(self) -> None:
        if self.order not in ORDERS:
            raise ConfigError(f"order must be one of {', '.join(ORDERS)}")
        if self.layer != "auto" and self.layer not in LAYERS:
            raise ConfigError(f"layer must be auto or one of {', '.join(LAYERS)}")
        if self.format not in FORMATS:
            raise ConfigError(f"format must be one of {', '.join(FORMATS)}")
        if not 1 <= self.concurrency <= 256:
            raise ConfigError("concurrency must be between 1 and 256")
        if self.limit < 0 or self.retries < 0 or self.slowest < 0:
            raise ConfigError("limit, retries and slowest cannot be negative")
        if self.rate < 0 or self.delay < 0 or self.verify_delay < 0:
            raise ConfigError("rate, delay and verify_delay cannot be negative")
        if self.timeout <= 0 or self.connect_timeout <= 0:
            raise ConfigError("timeouts must be positive")
        if self.min_hit_rate is not None and not 0 <= self.min_hit_rate <= 100:
            raise ConfigError("min_hit_rate must be between 0 and 100")
        for pattern in [*self.include, *self.exclude]:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(f"bad regular expression {pattern!r}: {exc}") from exc
        for rule in self.resolve:
            try:
                ResolveRule.parse(rule)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        for name, value in self.headers.items():
            try:
                parse_header(f"{name}: {value}")
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        self.since_datetime()

    def since_datetime(self, now: datetime | None = None) -> datetime | None:
        """``since`` as a datetime: an ISO date (``2026-11-01``) or a relative age (``36h``, ``7d``, ``2w``)."""
        if not self.since:
            return None
        text = self.since.strip().lower()
        match = re.fullmatch(r"(\d+)\s*([hdw])", text)
        if match:
            amount = int(match.group(1))
            delta = {"h": timedelta(hours=amount), "d": timedelta(days=amount), "w": timedelta(weeks=amount)}
            return (now or datetime.now(timezone.utc)) - delta[match.group(2)]
        from sitemap_cache_warmer.sitemap import parse_lastmod

        parsed = parse_lastmod(self.since)
        if parsed is None:
            raise ConfigError(f"since must be a date like 2026-11-01 or an age like 7d, got {self.since!r}")
        return parsed

    def resolve_rules(self) -> list[ResolveRule]:
        return [ResolveRule.parse(r) for r in self.resolve]


_SCALARS = {f.name: f for f in fields(Config) if f.name != "custom_profiles"}


def config_from_dict(data: dict[str, Any]) -> Config:
    unknown = sorted(set(data) - {"warm", "profiles"})
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(unknown)} (settings go under [warm])")
    warm = data.get("warm", {})
    if not isinstance(warm, dict):
        raise ConfigError("[warm] must be a table")
    bad = sorted(set(warm) - set(_SCALARS))
    if bad:
        raise ConfigError(f"unknown key(s) in [warm]: {', '.join(bad)}")
    config = Config()
    for key, value in warm.items():
        default = getattr(config, key)
        if isinstance(value, str) and key in ("sitemaps", "include", "exclude", "profiles", "resolve"):
            value = [value]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"warm.{key} must be true or false")
        elif isinstance(default, list):
            if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                raise ConfigError(f"warm.{key} must be a list of strings")
        elif isinstance(default, dict):
            if not (isinstance(value, dict) and all(isinstance(v, str) for v in value.values())):
                raise ConfigError(f"warm.{key} must be a table of strings")
        elif isinstance(default, int) and key not in ("min_hit_rate", "max_errors"):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConfigError(f"warm.{key} must be an integer")
        elif isinstance(default, float) or key in ("min_hit_rate", "max_errors"):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ConfigError(f"warm.{key} must be a number")
        elif isinstance(default, str) and not isinstance(value, str):
            raise ConfigError(f"warm.{key} must be a string")
        setattr(config, key, value)
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError("[profiles] must be a table")
    for name, spec in profiles.items():
        if not isinstance(spec, dict) or set(spec) - {"headers"} or not isinstance(spec.get("headers", {}), dict):
            raise ConfigError(f"[profiles.{name}] must contain only a 'headers' table")
        headers = {str(k): str(v) for k, v in spec.get("headers", {}).items()}
        for key, value in headers.items():
            try:
                parse_header(f"{key}: {value}")
            except ValueError as exc:
                raise ConfigError(f"[profiles.{name}]: {exc}") from exc
        config.custom_profiles[name] = Profile(name, headers)
    config.validate()
    return config


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return config_from_dict(data)

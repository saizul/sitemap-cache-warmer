"""Per-host pacing: a minimum interval between the starts of two requests to the same host."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class HostRateLimiter:
    """Spaces requests to each host by ``max(1 / rate, delay)`` seconds.

    ``rate`` is requests per second per host (0 = unlimited) and ``delay`` a fixed pause between requests to the
    same host. Requests to different hosts do not wait for each other.
    """

    def __init__(
        self,
        rate: float = 0.0,
        delay: float = 0.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        if rate < 0 or delay < 0:
            raise ValueError("rate and delay cannot be negative")
        self.interval = max(1.0 / rate if rate else 0.0, delay)
        self._next: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._clock = clock
        self._sleep = sleep

    async def wait(self, host: str) -> float:
        """Wait for the host's next slot; returns the time waited."""
        if self.interval <= 0:
            return 0.0
        async with self._lock:
            now = self._clock()
            start = max(now, self._next.get(host, now))
            self._next[host] = start + self.interval
        waited = start - now
        if waited > 0:
            await self._sleep(waited)  # type: ignore[misc]
        return waited

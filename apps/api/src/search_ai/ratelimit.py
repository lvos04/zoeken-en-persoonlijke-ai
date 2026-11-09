"""Eenvoudige verzoeklimiet per gebruiker (B07 §3: rate limits), in het geheugen van het API-proces.

Bedoeld als tweede verdedigingslinie naast de Hub: één gebruiker (of een misbruikt token) kan de NAS niet met
zoekopdrachten platleggen. Bij overschrijding → ``429 rate_limited`` met ``Retry-After``; er wordt dan niets gezocht.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

from .errors import ApiError


class RateLimiter:
    def __init__(
        self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000
    ) -> None:
        self.limit, self.window_s, self.clock, self.max_keys = limit, window_s, clock, max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = self.clock()
        with self._lock:
            if len(self._hits) > self.max_keys:  # geheugen begrenzen: verlopen sleutels opruimen
                for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window_s]:
                    del self._hits[k]
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                retry = max(1, math.ceil(self.window_s - (now - hits[0])))
                raise ApiError(
                    429,
                    "rate_limited",
                    "Te veel zoekopdrachten in korte tijd; probeer het over enkele seconden opnieuw.",
                    {"retry_after_s": retry, "limiet": self.limit, "venster_s": int(self.window_s)},
                    {"Retry-After": str(retry)},
                )
            hits.append(now)

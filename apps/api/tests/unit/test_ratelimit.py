"""Verzoeklimiet per gebruiker (B07 §3): schuivend venster, Retry-After, begrensd geheugen."""

from __future__ import annotations

import pytest

from search_ai.errors import ApiError
from search_ai.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_sliding_window_per_key_and_retry_after() -> None:
    clock = Clock()
    rl = RateLimiter(limit=2, window_s=10, clock=clock)
    rl.check("user:a")
    clock.t += 4
    rl.check("user:a")
    with pytest.raises(ApiError) as exc:
        rl.check("user:a")
    assert exc.value.status_code == 429 and exc.value.code == "rate_limited"
    assert exc.value.headers["Retry-After"] == "6"  # oudste treffer valt over 6 s uit het venster
    rl.check("user:b")  # andere gebruiker: eigen teller
    clock.t += 6.5
    rl.check("user:a")  # oudste treffer is verlopen


def test_memory_is_bounded() -> None:
    clock = Clock()
    rl = RateLimiter(limit=1, window_s=1, clock=clock, max_keys=10)
    for i in range(50):
        rl.check(f"user:{i}")
        clock.t += 2  # elke vorige sleutel is dan al verlopen
    assert len(rl._hits) <= 12

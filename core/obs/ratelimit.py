"""Central token-bucket rate limiter, shared across parallel jobs.

One bucket per provider (built from config.PROVIDER_LIMITS) so that N concurrent
match jobs can't collectively exceed a free-tier rate. Clock is injectable for
deterministic tests.
"""
from __future__ import annotations
import threading
import time
from config import observability as cfg


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float, now=time.monotonic):
        self.rate = rate_per_sec
        self.capacity = capacity
        self.tokens = capacity
        self._now = now
        self._ts = now()
        self._lock = threading.Lock()

    def _refill(self):
        t = self._now()
        self.tokens = min(self.capacity, self.tokens + (t - self._ts) * self.rate)
        self._ts = t

    def try_acquire(self, n: float = 1) -> bool:
        with self._lock:
            self._refill()
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False

    def acquire(self, n: float = 1, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else self._now() + timeout
        while True:
            if self.try_acquire(n):
                return True
            if deadline is not None and self._now() >= deadline:
                return False
            time.sleep(min(0.05, n / self.rate if self.rate else 0.05))


_BUCKETS: dict[str, TokenBucket] = {}
_lock = threading.Lock()


def bucket(provider: str) -> TokenBucket:
    with _lock:
        b = _BUCKETS.get(provider)
        if b is None:
            lim = cfg.PROVIDER_LIMITS.get(provider, {"rate": 1, "per": 1})
            rate = lim["rate"] / lim["per"]
            b = _BUCKETS[provider] = TokenBucket(rate_per_sec=rate,
                                                 capacity=max(1, lim["rate"]))
        return b


def acquire(provider: str, n: float = 1, timeout: float | None = 30) -> bool:
    """Block (up to timeout) until the provider's rate budget allows the call."""
    return bucket(provider).acquire(n, timeout)

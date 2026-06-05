"""Resilience helpers: retry-with-backoff and ordered fallback.

Best practice (verified): retry 2-3× on *transient* errors with exponential
backoff + jitter; fail fast on *permanent* errors; surface attempts to logging.
Dependency-light so it runs offline and is easy to test (inject `sleep`). In
production you may swap in `tenacity`/`pybreaker` — the call sites won't change.
"""
from __future__ import annotations
import functools
import random
import time

from core.obs.logging import get_logger
from core.obs import metrics

log = get_logger("reliability")


class PermanentError(Exception):
    """Raise for non-retryable failures (bad input, auth) -> fail fast."""


# Transient = worth retrying. Network/timeouts/OS errors by default; extend as needed.
TRANSIENT_EXC = (ConnectionError, TimeoutError, OSError)
TRANSIENT_HTTP = {429, 500, 502, 503, 504}


def is_transient(exc: Exception) -> bool:
    if isinstance(exc, PermanentError):
        return False
    if isinstance(exc, TRANSIENT_EXC):
        return True
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return code in TRANSIENT_HTTP


def retry(max_attempts: int = 3, base: float = 0.5, factor: float = 2.0,
          jitter: float = 0.1, sleep=time.sleep, on=is_transient):
    """Retry a callable on transient failures with exponential backoff + jitter."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            attempt = 0
            while True:
                attempt += 1
                try:
                    return fn(*a, **kw)
                except Exception as e:  # noqa: BLE001
                    if attempt >= max_attempts or not on(e):
                        metrics.incr("retry_exhausted", 1, fn=fn.__name__)
                        raise
                    delay = base * (factor ** (attempt - 1)) + random.uniform(0, jitter)
                    log.warning("transient error in %s (attempt %d/%d): %s; retrying in %.2fs",
                                fn.__name__, attempt, max_attempts, e, delay)
                    metrics.incr("retry_attempt", 1, fn=fn.__name__)
                    sleep(delay)
        return wrapper
    return deco


def with_fallback(*callables, label: str = "operation"):
    """Call each zero-arg callable in order; return the first success.

    Use for source fallback, e.g. football-data -> API-Football.
    """
    last = None
    for i, fn in enumerate(callables):
        try:
            result = fn()
            if i > 0:
                log.warning("%s succeeded via fallback #%d (%s)", label, i,
                            getattr(fn, "__name__", "fn"))
                metrics.incr("fallback_used", 1, op=label)
            return result
        except Exception as e:  # noqa: BLE001
            log.warning("%s source #%d failed: %s", label, i, e)
            last = e
    raise RuntimeError(f"all sources failed for {label}; last error: {last}")

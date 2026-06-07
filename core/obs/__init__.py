"""Observability facade — one import for the whole layer.

    from core import obs
    obs.setup()                              # call once at startup
    with obs.run("match-401-T7m"):           # correlation id for the whole job
        with obs.external_call("odds_api", "h2h"):   # rate-limit + trace + cost
            ...

`external_call` does the three things every outbound call needs, in best-practice
order: acquire the shared rate-limit token, open a trace span, time it, and record
the call (+ tokens) in the cost/quota ledger.
"""
from __future__ import annotations
import time
import uuid
from contextlib import contextmanager

from contextvars import ContextVar
from core.obs.logging import get_logger, setup_logging, correlation_id
from core.obs.tracing import span, traced
from core.obs import metrics, ratelimit
from core.obs.cost import ledger

log = get_logger("obs")

# the stage currently executing within a job — used to attribute failures
current_stage: ContextVar[str] = ContextVar("current_stage", default="-")


@contextmanager
def staged(name: str, **attrs):
    """Mark a pipeline stage (odds/model/news/...) so a failure can be attributed
    to it, and open a trace span for it. Tags any escaping exception with the
    (innermost) stage so the pipeline can record where it failed."""
    token = current_stage.set(name)
    try:
        with span(f"stage:{name}", **attrs):
            yield name
    except Exception as e:  # noqa: BLE001
        if not getattr(e, "_mondial_stage", None):
            try:
                e._mondial_stage = name
            except Exception:
                pass
        raise
    finally:
        current_stage.reset(token)


def stage_of(exc: BaseException) -> str:
    """The stage an exception failed in (set by `staged`), or '-'."""
    return getattr(exc, "_mondial_stage", None) or "-"


def setup() -> None:
    """Initialise logging (and lazily, tracing/metrics on first use)."""
    setup_logging()
    log.info("observability initialised")


@contextmanager
def run(label: str | None = None):
    """Set a correlation id for an entire match-window job."""
    cid = label or uuid.uuid4().hex[:12]
    token = correlation_id.set(cid)
    with span("run", correlation_id=cid):
        try:
            yield cid
        finally:
            correlation_id.reset(token)


@contextmanager
def external_call(provider: str, endpoint: str, tokens: int = 0,
                  units: float = 1, rate_timeout: float | None = 30):
    """Guard one outbound API/LLM call: rate-limit → span → time → cost.

    On exception the cost-ledger row is stamped with `error_class` (e.g.
    'RateLimitError', 'APIConnectionError', 'AuthenticationError') and
    `error_message` (first 200 chars of the exception repr) so root-cause
    is queryable later — no need to grep journalctl to figure out *why*
    Gemini failed at 14:32 yesterday."""
    if not ratelimit.acquire(provider, n=units, timeout=rate_timeout):
        log.warning("rate-limit timeout for %s/%s", provider, endpoint)
    start = time.monotonic()
    ok = True
    err_class: str | None = None
    err_msg: str | None = None
    try:
        with span(f"{provider}.{endpoint}", provider=provider, endpoint=endpoint):
            yield
    except Exception as e:                              # noqa: BLE001
        ok = False
        err_class = type(e).__name__
        err_msg = str(e)
        raise
    finally:
        dur_ms = (time.monotonic() - start) * 1000
        metrics.observe("external_call_ms", dur_ms, provider=provider, endpoint=endpoint)
        ledger().record(provider, endpoint, units=units, tokens=tokens, ok=ok,
                        correlation_id=correlation_id.get(), duration_ms=dur_ms,
                        error_class=err_class, error_message=err_msg)


__all__ = ["setup", "run", "staged", "stage_of", "current_stage", "external_call",
           "span", "traced", "metrics", "ratelimit", "ledger", "get_logger", "log"]

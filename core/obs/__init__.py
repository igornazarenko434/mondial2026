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


def _classify_exception(e: BaseException) -> tuple[int | None, str | None, str]:
    """Extract (status_code, retry_after, error_kind) from a provider-SDK
    exception so the cost-ledger row can distinguish 401 vs 429 vs 503 vs
    Cloudflare-HTML vs requests.Timeout vs ConnectionError. Day-9.11."""
    # status code — surfaces on requests.HTTPError.response.status_code, on
    # anthropic.APIStatusError.status_code, openai.APIStatusError.status_code,
    # google.api_core.exceptions.* .code() (callable). Be defensive.
    sc = None
    try:
        resp = getattr(e, "response", None)
        sc = getattr(resp, "status_code", None) if resp is not None else None
        if sc is None:
            sc = getattr(e, "status_code", None)
        if sc is None and hasattr(e, "code") and callable(e.code):
            sc = e.code()
    except Exception:                                  # noqa: BLE001
        sc = None
    if not isinstance(sc, int):
        sc = None
    # retry-after — only meaningful on 429
    ra: str | None = None
    try:
        resp = getattr(e, "response", None)
        headers = getattr(resp, "headers", {}) or {}
        ra = headers.get("Retry-After") if isinstance(headers, dict) else None
    except Exception:                                  # noqa: BLE001
        ra = None
    # error-kind classification
    name = type(e).__name__.lower()
    if "timeout" in name:
        kind = "timeout"
    elif "connection" in name and "http" not in name:
        kind = "network"
    elif sc is not None:
        kind = "http"
    else:
        kind = "other"
    return sc, ra, kind


@contextmanager
def external_call(provider: str, endpoint: str, tokens: int = 0,
                  units: float = 1, rate_timeout: float | None = 30):
    """Guard one outbound API/LLM call: rate-limit → span → time → cost.

    On exception the cost-ledger row is stamped with:
      - error_class    (type(e).__name__)
      - error_message  (first 200 chars of str(e))
      - status_code    (HTTP status if available — 401/429/503)
      - retry_after    (Retry-After header value if any)
      - error_kind     ('http' / 'timeout' / 'network' / 'ratelimit_timeout' / 'other')

    Day-9.11: the rate-limit acquire now FAILS CLOSED — if the local token
    bucket can't be acquired within rate_timeout, we raise RateLimitTimeout
    BEFORE entering the body (was: log warning + proceed, which produced a
    downstream 429 indistinguishable from a real upstream one).

    Day-9.13 fix: ratelimit and credit accounting are independent concerns.
    Rate-limit is about REQUEST FREQUENCY (one HTTP call = one token; the
    bucket throttles politeness to the API). Credits are about QUOTA
    (a multi-region/-market outright call can cost 2-4 credits per call).
    Previously we passed `units` to BOTH — so a 2-credit outright call
    needed 2 tokens, but the bucket has capacity=1 → always failed. Now
    rate-limit always consumes 1 token; ledger.record still records the
    full credit cost."""
    if not ratelimit.acquire(provider, n=1, timeout=rate_timeout):
        log.warning("rate-limit timeout for %s/%s — failing closed", provider, endpoint)
        metrics.incr("rate_limit_timeout", 1, provider=provider)
        # Record the would-be call so the audit shows the local-bucket block.
        try:
            ledger().record(provider, endpoint, units=units, tokens=tokens, ok=False,
                            correlation_id=correlation_id.get(), duration_ms=0,
                            error_class="RateLimitTimeout",
                            error_message=f"local token bucket blocked {provider}/{endpoint}",
                            error_kind="ratelimit_timeout")
        except Exception:                              # noqa: BLE001
            pass
        raise RateLimitTimeout(f"{provider}/{endpoint} blocked by local bucket")
    start = time.monotonic()
    ok = True
    err_class: str | None = None
    err_msg: str | None = None
    status_code: int | None = None
    retry_after: str | None = None
    error_kind: str | None = None
    try:
        with span(f"{provider}.{endpoint}", provider=provider, endpoint=endpoint):
            yield
    except Exception as e:                              # noqa: BLE001
        ok = False
        err_class = type(e).__name__
        err_msg = str(e)
        status_code, retry_after, error_kind = _classify_exception(e)
        raise
    finally:
        dur_ms = (time.monotonic() - start) * 1000
        metrics.observe("external_call_ms", dur_ms, provider=provider, endpoint=endpoint)
        ledger().record(provider, endpoint, units=units, tokens=tokens, ok=ok,
                        correlation_id=correlation_id.get(), duration_ms=dur_ms,
                        error_class=err_class, error_message=err_msg,
                        status_code=status_code, retry_after=retry_after,
                        error_kind=error_kind)


class RateLimitTimeout(Exception):
    """Local token-bucket couldn't be acquired within `rate_timeout`. Raised
    by `external_call` so the caller (LLMRouter) can attribute the failure
    to OUR local throttle rather than an upstream rate limit. Day-9.11."""
    pass


__all__ = ["setup", "run", "staged", "stage_of", "current_stage", "external_call",
           "span", "traced", "metrics", "ratelimit", "ledger", "get_logger", "log",
           "RateLimitTimeout"]

"""Distributed tracing via OpenTelemetry (vendor-neutral industry standard).

Exporter is configurable (console / OTLP to Jaeger·Tempo·Honeycomb / none). If
the OTel SDK isn't installed, everything degrades to safe no-ops so the system
runs identically without observability deps.

Use:
    from core.obs.tracing import traced, span
    @traced("model.blend")
    def f(...): ...
    with span("odds.pull", match_id=401):
        ...
"""
from __future__ import annotations
import functools
from contextlib import contextmanager
from config import observability as cfg

_tracer = None
_init = False


def _setup():
    global _tracer, _init
    if _init:
        return
    _init = True
    if not cfg.ENABLED or cfg.TRACES_EXPORTER == "none":
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
        provider = TracerProvider(resource=Resource.create({"service.name": cfg.SERVICE_NAME}))
        if cfg.TRACES_EXPORTER == "otlp" and cfg.OTLP_ENDPOINT:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=f"{cfg.OTLP_ENDPOINT}/v1/traces")
        else:
            exporter = ConsoleSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(cfg.SERVICE_NAME)
    except Exception:
        _tracer = None  # OTel not installed -> no-op


@contextmanager
def span(name: str, **attrs):
    """Open an OTel span. Day-9.11: auto-stamps `correlation_id` and `stage`
    on every span so a single Honeycomb query `WHERE correlation_id = X`
    returns the full tree (run → stage:news → gemini.complete) instead of
    just the root."""
    _setup()
    if _tracer is None:
        yield None
        return
    # Read context-var values eagerly so we never crash on an empty stack.
    try:
        from core.obs.logging import correlation_id as _cid
        cid = _cid.get()
    except Exception:                                  # noqa: BLE001
        cid = "-"
    try:
        from core.obs import current_stage as _stg
        stg = _stg.get()
    except Exception:                                  # noqa: BLE001
        stg = "-"
    with _tracer.start_as_current_span(name) as sp:
        # Auto-stamp BEFORE caller attrs so the caller can override if needed.
        if cid and cid != "-":
            sp.set_attribute("correlation_id", cid)
        if stg and stg != "-":
            sp.set_attribute("stage", stg)
        for k, v in attrs.items():
            sp.set_attribute(k, v)
        yield sp


def traced(name: str | None = None):
    def deco(fn):
        sname = name or f"{fn.__module__}.{fn.__name__}"
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            with span(sname):
                return fn(*a, **kw)
        return wrapper
    return deco

"""Lightweight metrics with an OpenTelemetry backend and a no-op fallback.

Counters (api calls, errors, llm tokens) and histograms (latency). If the OTel
metrics SDK isn't present, calls are silently dropped — instrumentation never
breaks the pipeline.
"""
from __future__ import annotations
from config import observability as cfg

_meter = None
_init = False
_instruments: dict = {}


def _setup():
    global _meter, _init
    if _init:
        return
    _init = True
    if not cfg.ENABLED:
        return
    try:
        from opentelemetry import metrics
        _meter = metrics.get_meter(cfg.SERVICE_NAME)
    except Exception:
        _meter = None


def incr(name: str, value: int = 1, **attrs):
    _setup()
    if _meter is None:
        return
    c = _instruments.get(name)
    if c is None:
        c = _instruments[name] = _meter.create_counter(name)
    c.add(value, attrs)


def observe(name: str, value: float, **attrs):
    _setup()
    if _meter is None:
        return
    h = _instruments.get(name)
    if h is None:
        h = _instruments[name] = _meter.create_histogram(name)
    h.record(value, attrs)

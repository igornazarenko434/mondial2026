"""Structured logging with correlation IDs.

JSON logs (best practice for machine parsing / shipping to a backend) with a
per-run correlation id and the active trace id injected automatically, so every
log line can be tied to the match-window job that produced it. No external dep:
falls back to stdlib with a small JSON formatter.
"""
from __future__ import annotations
import json
import logging
import sys
from contextvars import ContextVar
from config import observability as cfg

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")
_configured = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": correlation_id.get(),
        }
        # current trace id, if tracing is active
        try:
            from opentelemetry import trace
            ctx = trace.get_current_span().get_span_context()
            if ctx and ctx.trace_id:
                payload["trace_id"] = format(ctx.trace_id, "032x")
        except Exception:
            pass
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in getattr(record, "extra_fields", {}).items():
            payload[k] = v
        return json.dumps(payload, default=str)


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    if cfg.LOG_JSON:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(cfg.LOG_LEVEL)
    _configured = True


def get_logger(name: str) -> logging.LoggerAdapter:
    setup_logging()
    return logging.getLogger(name)

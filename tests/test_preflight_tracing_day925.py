"""Day-9.25: pin the OTel tracing preflight self-test.

Loud-at-startup self-test so missing/garbled Honeycomb config doesn't slip
through and silently no-op spans for the whole tournament.
"""
from __future__ import annotations
import pytest

from config.preflight import _check_tracing


def test_tracing_off_when_traces_exporter_none(monkeypatch):
    """TRACES_EXPORTER=none → intentional off → return True (not a failure)."""
    monkeypatch.setattr("config.observability.ENABLED", True, raising=False)
    monkeypatch.setattr("config.observability.TRACES_EXPORTER", "none",
                         raising=False)
    assert _check_tracing() is True


def test_tracing_console_exporter_returns_true(monkeypatch):
    """Console exporter needs no remote — return True."""
    monkeypatch.setattr("config.observability.ENABLED", True, raising=False)
    monkeypatch.setattr("config.observability.TRACES_EXPORTER", "console",
                         raising=False)
    assert _check_tracing() is True


def test_tracing_otlp_without_endpoint_returns_false(monkeypatch):
    """OTLP requested but endpoint blank → traces would no-op silently.
    Loud error at preflight + False return."""
    monkeypatch.setattr("config.observability.ENABLED", True, raising=False)
    monkeypatch.setattr("config.observability.TRACES_EXPORTER", "otlp",
                         raising=False)
    monkeypatch.setattr("config.observability.OTLP_ENDPOINT", "",
                         raising=False)
    assert _check_tracing() is False


def test_tracing_honeycomb_without_auth_header_returns_false(monkeypatch):
    """Honeycomb endpoint but no x-honeycomb-team API key header → spans
    rejected by the receiver. Pre-flight catches it."""
    monkeypatch.setattr("config.observability.ENABLED", True, raising=False)
    monkeypatch.setattr("config.observability.TRACES_EXPORTER", "otlp",
                         raising=False)
    monkeypatch.setattr("config.observability.OTLP_ENDPOINT",
                         "https://api.honeycomb.io", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    assert _check_tracing() is False


def test_tracing_otlp_with_headers_runs_healthcheck(monkeypatch):
    """OTLP + headers configured → preflight opens a no-op span successfully."""
    monkeypatch.setattr("config.observability.ENABLED", True, raising=False)
    monkeypatch.setattr("config.observability.TRACES_EXPORTER", "otlp",
                         raising=False)
    monkeypatch.setattr("config.observability.OTLP_ENDPOINT",
                         "https://api.honeycomb.io", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-honeycomb-team=abc")
    # Tracing's span() degrades to nullcontext if SDK isn't installed
    # locally; either way the open/close cycle should not raise.
    assert _check_tracing() is True


def test_tracing_unknown_exporter_warns(monkeypatch):
    """Unknown TRACES_EXPORTER value → return False (we treat as off)."""
    monkeypatch.setattr("config.observability.ENABLED", True, raising=False)
    monkeypatch.setattr("config.observability.TRACES_EXPORTER", "garbage",
                         raising=False)
    assert _check_tracing() is False


def test_tracing_included_in_check_status(monkeypatch):
    """`tracing` key appears in the preflight status dict."""
    from config import preflight as pf
    monkeypatch.setattr("config.observability.TRACES_EXPORTER", "none",
                         raising=False)
    status = pf.check()
    assert "tracing" in status

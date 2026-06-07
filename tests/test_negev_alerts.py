"""Negev MCP failure-alert helper (Day-9.9)."""
from __future__ import annotations

import pytest

from integrations import negev_alerts


def test_classify_config():
    cat, hint = negev_alerts.classify("NEGEV_TOURNAMENT_ID not set")
    assert cat == "config"
    assert ".env" in hint


def test_classify_auth_zero_rows():
    cat, hint = negev_alerts.classify(
        "Negev returned 0 rows — auth failed or tournament empty")
    assert cat == "auth"
    assert "refreshToken" in hint or "refresh-token" in hint


def test_classify_auth_401():
    cat, hint = negev_alerts.classify("HTTP 401 Unauthorized from securetoken")
    assert cat == "auth"


def test_classify_rules_403():
    cat, hint = negev_alerts.classify("HTTP 403: Missing or insufficient permissions")
    assert cat == "rules"
    assert "verify_negev_live" in hint or "rules" in hint.lower()


def test_classify_network():
    cat, _ = negev_alerts.classify("Connection timeout after 30s")
    assert cat == "network"


def test_classify_import_error():
    cat, _ = negev_alerts.classify(
        "Negev MCP module not importable: No module named 'integrations.negev_toto_mcp'")
    assert cat == "import"


def test_classify_unknown_falls_through():
    cat, _ = negev_alerts.classify("some weird thing nobody planned for")
    assert cat == "unknown"


def test_classify_empty_string_doesnt_crash():
    cat, _ = negev_alerts.classify("")
    assert cat == "unknown"


def test_alert_failure_sends_telegram_with_classification(monkeypatch):
    """alert_failure should call delivery.alert with category + hint in body."""
    captured: dict = {}

    def fake_alert(title: str, body: str) -> bool:
        captured["title"] = title
        captured["body"] = body
        return True

    monkeypatch.setattr("core.delivery.alert", fake_alert)

    ok = negev_alerts.alert_failure(
        source="sync_negev_standings",
        reason="NEGEV_TOURNAMENT_ID not set")

    assert ok is True
    assert "config" in captured["title"]              # category in title
    assert "sync_negev_standings" in captured["body"]
    assert "NEGEV_TOURNAMENT_ID" in captured["body"]
    assert ".env" in captured["body"]                  # hint included


def test_alert_failure_truncates_long_reasons(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("core.delivery.alert",
                        lambda t, b: captured.setdefault("body", b) or True)

    very_long = "x" * 5000
    negev_alerts.alert_failure(source="test", reason=very_long)
    # 400-char cap on the reason block; rest of the body adds wrapper text
    assert len(captured["body"]) < 1500


def test_alert_failure_returns_false_when_delivery_fails(monkeypatch):
    monkeypatch.setattr("core.delivery.alert", lambda t, b: False)
    assert negev_alerts.alert_failure(source="x", reason="y") is False


def test_alert_failure_swallows_delivery_exceptions(monkeypatch):
    """Telegram itself being down must NOT raise from the caller's perspective."""
    def boom(*args, **kwargs):
        raise RuntimeError("Telegram API 503")

    monkeypatch.setattr("core.delivery.alert", boom)
    # Should not raise; should return False
    assert negev_alerts.alert_failure(source="x", reason="y") is False

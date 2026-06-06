"""Day-8 Brave free-tier protection — multi-gate budget guard.

Verifies that web_search() short-circuits BEFORE the HTTP call when:
  1. the monthly hard-brake is reached (95% of 2000)
  2. the rolling-24h soft cap is reached (BRAVE_DAILY_LIMIT)
without burning the call against the cost ledger.
"""
from __future__ import annotations
from unittest.mock import MagicMock, patch
import pytest

import core.data.web_search as ws


def _set_brave_key(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")


def test_no_key_returns_empty_without_calling_brave(monkeypatch):
    """L0 short-circuit: no key, no call."""
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    with patch("core.data.web_search.requests.get") as g:
        out = ws.web_search("anything")
    assert out == []
    g.assert_not_called()


def test_monthly_brake_at_90pct_blocks_call(monkeypatch):
    """L1 monthly-fraction brake: at >=90% used, web_search returns [] without
    hitting the API even though the key IS configured. (Budget is 1000 to match
    Brave's $5/mo free credit = 1,000 requests.)"""
    _set_brave_key(monkeypatch)
    monkeypatch.setattr(ws, "_day_count", lambda: 0)
    fake_ledger = MagicMock()
    fake_ledger.quota_status.return_value = {"used": 950, "budget": 1000}  # 95%
    monkeypatch.setattr("core.obs.cost.ledger", lambda: fake_ledger)
    with patch("core.data.web_search.requests.get") as g:
        out = ws.web_search("anything")
    assert out == []
    g.assert_not_called()


def test_below_monthly_brake_allows_call(monkeypatch):
    """At 70% used (under the 90% brake) the call is allowed."""
    _set_brave_key(monkeypatch)
    monkeypatch.setattr(ws, "_day_count", lambda: 0)
    fake_ledger = MagicMock()
    fake_ledger.quota_status.return_value = {"used": 700, "budget": 1000}  # 70%
    monkeypatch.setattr("core.obs.cost.ledger", lambda: fake_ledger)
    # Patch the actual HTTP so the test stays offline
    fake_resp = MagicMock(ok=True, status_code=200)
    fake_resp.json.return_value = {"web": {"results": [
        {"title": "t", "description": "s", "url": "u", "page_age": "2026-06-11"}]}}
    fake_resp.raise_for_status = MagicMock()
    with patch("core.data.web_search.requests.get", return_value=fake_resp):
        out = ws.web_search("anything", n=1)
    assert len(out) == 1


def test_daily_soft_cap_blocks_call(monkeypatch):
    """L2 daily soft-cap: even with monthly headroom, rolling-24h cap blocks.
    Default daily limit is 60 (matches 60 × 30d ≈ 1800 monthly budget, but the
    monthly budget itself is 1,000 → effective ~33/day average over month)."""
    _set_brave_key(monkeypatch)
    monkeypatch.setattr("config.news.BRAVE_DAILY_LIMIT", 60)
    monkeypatch.setattr(ws, "BRAVE_DAILY_LIMIT", 60)
    monkeypatch.setattr(ws, "_day_count", lambda: 60)   # at the cap
    fake_ledger = MagicMock()
    fake_ledger.quota_status.return_value = {"used": 100, "budget": 1000}
    monkeypatch.setattr("core.obs.cost.ledger", lambda: fake_ledger)
    with patch("core.data.web_search.requests.get") as g:
        out = ws.web_search("anything")
    assert out == []
    g.assert_not_called()


def test_daily_soft_cap_disabled_when_zero(monkeypatch):
    """BRAVE_DAILY_LIMIT=0 disables the daily gate entirely."""
    _set_brave_key(monkeypatch)
    monkeypatch.setattr(ws, "BRAVE_DAILY_LIMIT", 0)
    monkeypatch.setattr(ws, "_day_count", lambda: 999)   # would normally block
    fake_ledger = MagicMock()
    fake_ledger.quota_status.return_value = {"used": 100, "budget": 1000}
    monkeypatch.setattr("core.obs.cost.ledger", lambda: fake_ledger)
    fake_resp = MagicMock(ok=True, status_code=200)
    fake_resp.json.return_value = {"web": {"results": []}}
    fake_resp.raise_for_status = MagicMock()
    with patch("core.data.web_search.requests.get", return_value=fake_resp) as g:
        out = ws.web_search("anything")
    g.assert_called_once()
    assert out == []  # legitimate empty response, not a guard-block


def test_quota_status_returns_complete_shape(monkeypatch):
    """The CLI / dashboard helper exposes all the fields a human needs."""
    _set_brave_key(monkeypatch)
    monkeypatch.setattr(ws, "_day_count", lambda: 12)
    fake_ledger = MagicMock()
    fake_ledger.quota_status.return_value = {"used": 200, "budget": 1000}
    monkeypatch.setattr("core.obs.cost.ledger", lambda: fake_ledger)
    q = ws.quota_status()
    assert set(q.keys()) >= {"month_used", "month_budget", "month_fraction",
                              "day_used", "day_limit", "ok", "key_set"}
    assert q["month_used"] == 200
    assert q["month_budget"] == 1000
    assert q["day_used"] == 12
    assert q["key_set"] is True
    assert q["ok"] is True


def test_quota_status_signals_blocked_when_over_brake(monkeypatch):
    _set_brave_key(monkeypatch)
    monkeypatch.setattr(ws, "_day_count", lambda: 0)
    fake_ledger = MagicMock()
    fake_ledger.quota_status.return_value = {"used": 950, "budget": 1000}  # 95%
    monkeypatch.setattr("core.obs.cost.ledger", lambda: fake_ledger)
    q = ws.quota_status()
    assert q["month_fraction"] >= 0.90
    assert q["ok"] is False

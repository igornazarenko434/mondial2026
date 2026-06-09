"""Day-9.21: Brave Search per-query in-memory cache + stale-on-budget."""
from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    """Reset module-level cache between tests."""
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "tok")
    from core.data import web_search as ws
    ws._BRAVE_CACHE.clear()
    yield
    ws._BRAVE_CACHE.clear()


def _resp(json_body, ok=True, status=200):
    r = MagicMock()
    r.ok = ok
    r.status_code = status
    r.json.return_value = json_body
    r.raise_for_status = MagicMock()
    if not ok:
        r.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
    return r


def test_brave_search_caches_within_ttl(monkeypatch):
    from core.data import web_search as ws
    monkeypatch.setattr(ws, "_budget_clear", lambda: (True, "ok"))
    body = {"web": {"results": [
        {"title": "T", "description": "desc", "url": "https://example.com/x",
         "page_age": "2026-06-09"}]}}
    call_count = {"n": 0}
    def fake_get(*a, **kw):
        call_count["n"] += 1
        return _resp(body)
    monkeypatch.setattr(ws.requests, "get", fake_get)
    r1 = ws.web_search("test query")
    r2 = ws.web_search("test query")
    r3 = ws.web_search("test query")
    assert r1 == r2 == r3
    assert call_count["n"] == 1, f"3 calls collapsed to {call_count['n']}"


def test_brave_search_returns_stale_when_budget_brake_hits(monkeypatch):
    """Day-9.21: budget exhausted AND we have cached results → return them
    (matches api_football's Day-9.20 stale fallback)."""
    from core.data import web_search as ws
    # Seed cache
    ws._BRAVE_CACHE[("test query", "pw", 5)] = (
        time.time() - 1, [{"title": "stale article",
                            "snippet": "...", "url": "x", "date": "2026-06-09"}])
    # Now flip the budget gate
    monkeypatch.setattr(ws, "_budget_clear", lambda: (False, "daily_cap"))
    out = ws.web_search("test query")
    assert len(out) == 1
    assert out[0]["title"] == "stale article", \
        "must serve stale when budget out"


def test_brave_search_returns_empty_when_budget_out_and_no_cache(monkeypatch):
    """No cache + budget out → empty (current behaviour preserved)."""
    from core.data import web_search as ws
    monkeypatch.setattr(ws, "_budget_clear", lambda: (False, "monthly_brake"))
    out = ws.web_search("fresh query no cache")
    assert out == []


def test_brave_search_cache_keyed_by_query_freshness_n(monkeypatch):
    """Different (query, freshness, n) tuples should cache independently."""
    from core.data import web_search as ws
    monkeypatch.setattr(ws, "_budget_clear", lambda: (True, "ok"))
    call_count = {"n": 0}
    def fake_get(*a, **kw):
        call_count["n"] += 1
        return _resp({"web": {"results": []}})
    monkeypatch.setattr(ws.requests, "get", fake_get)
    ws.web_search("A")
    ws.web_search("A", n=10)                          # different n
    ws.web_search("A", freshness="pd")                # different freshness
    ws.web_search("A")                                # cache hit
    assert call_count["n"] == 3, \
        f"3 distinct keys + 1 hit; expected 3 api calls, got {call_count['n']}"


def test_brave_search_expired_cache_refetches(monkeypatch):
    from core.data import web_search as ws
    monkeypatch.setattr(ws, "_budget_clear", lambda: (True, "ok"))
    monkeypatch.setattr(ws, "BRAVE_CACHE_TTL_SEC", 1)
    # Seed with ancient timestamp
    ws._BRAVE_CACHE[("q", "pw", 5)] = (time.time() - 10,
                                        [{"title": "stale"}])
    call_count = {"n": 0}
    def fake_get(*a, **kw):
        call_count["n"] += 1
        return _resp({"web": {"results": [
            {"title": "fresh", "description": "", "url": "u",
             "page_age": "2026-06-09"}]}})
    monkeypatch.setattr(ws.requests, "get", fake_get)
    out = ws.web_search("q")
    assert call_count["n"] == 1
    assert out[0]["title"] == "fresh"


def test_brave_search_caches_empty_responses_too(monkeypatch):
    from core.data import web_search as ws
    monkeypatch.setattr(ws, "_budget_clear", lambda: (True, "ok"))
    call_count = {"n": 0}
    def fake_get(*a, **kw):
        call_count["n"] += 1
        return _resp({"web": {"results": []}})
    monkeypatch.setattr(ws.requests, "get", fake_get)
    ws.web_search("zero results query")
    ws.web_search("zero results query")
    ws.web_search("zero results query")
    assert call_count["n"] == 1, \
        "empty results must be cached so we don't re-query for the same nothing"

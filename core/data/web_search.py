"""Web-search adapter for the news agent (Day 8).

Currently wires Brave Search API (https://api.search.brave.com). Free tier:
2,000 queries/month, JSON response. The wrapper is fully OPTIONAL — if
`BRAVE_SEARCH_API_KEY` isn't set, every call returns []. The news agent then
proceeds with API-Football only.

Usage:
    from core.data.web_search import web_search
    results = web_search("Norway vs France WC 2026 lineup 2026-06-26", n=5)
    # → [{"title": "...", "snippet": "...", "url": "...", "date": "2026-06-26"}]

Returns at most `n` results, each ≤ ~250 chars of snippet to keep our LLM
context budget bounded (see config/news.py::SNIPPET_LEN).
"""
from __future__ import annotations
import os
import time
from datetime import datetime, timezone
from typing import Any
import requests

from core.obs.logging import get_logger

log = get_logger("data.web_search")

BRAVE_API_BASE = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_FRESHNESS = "pw"      # "past week" — matches our NEWS_RECENCY_HOURS=48 ceiling


def available() -> bool:
    """True iff a Brave key is configured. Caller can short-circuit."""
    return bool(os.environ.get("BRAVE_SEARCH_API_KEY"))


def _headers() -> dict | None:
    key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not key:
        return None
    return {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": key,
    }


def _trim(s: str, n: int) -> str:
    """Compact snippet — strip whitespace + cap length."""
    return " ".join((s or "").split())[:n]


def _parse_date(s: str | None) -> str | None:
    """Brave sometimes gives ISO timestamps, sometimes 'X days ago'. Best-effort
    to surface a YYYY-MM-DD so the LLM can date-relevance-filter."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")) \
                       .astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


def web_search(query: str, n: int = 5, snippet_len: int = 250,
               freshness: str = DEFAULT_FRESHNESS) -> list[dict]:
    """Run one Brave search query. Returns []  if no key or any failure.

    Args:
      query:       the search string
      n:           max results to return (Brave returns up to 20; we cap)
      snippet_len: per-result snippet character cap (token-budget control)
      freshness:   Brave's freshness filter — 'pd'=past day, 'pw'=past week,
                   'pm'=past month, 'py'=past year. Default 'pw' matches our
                   NEWS_RECENCY_HOURS=48 ceiling.
    """
    h = _headers()
    if not h:
        return []
    from core import obs
    params = {"q": query, "count": max(1, min(n, 20)), "freshness": freshness,
              "safesearch": "moderate", "result_filter": "web"}
    try:
        with obs.external_call("brave_search", "web", units=1):
            resp = requests.get(BRAVE_API_BASE, headers=h, params=params, timeout=15)
            resp.raise_for_status()
    except Exception as e:                                  # noqa: BLE001
        log.warning("brave_search failed: %s", e)
        return []
    try:
        body = resp.json()
    except ValueError:
        log.warning("brave_search returned non-JSON")
        return []
    results = []
    for item in (body.get("web") or {}).get("results", [])[:n]:
        results.append({
            "title":   _trim(item.get("title", ""),       120),
            "snippet": _trim(item.get("description", ""), snippet_len),
            "url":     item.get("url", "") or "",
            "date":    _parse_date(item.get("page_age")),
        })
    return results


def web_search_many(queries: list[str], n: int = 5, snippet_len: int = 250,
                     freshness: str = DEFAULT_FRESHNESS,
                     pacing_sec: float = 1.1) -> list[dict]:
    """Run multiple queries serially with rate-limit-friendly pacing (Brave free
    tier = 1 req/sec). Returns the FLAT, deduplicated result list across all
    queries — duplicates collapsed by URL."""
    seen_urls: set[str] = set()
    out: list[dict] = []
    for i, q in enumerate(queries):
        if i > 0 and pacing_sec > 0:
            time.sleep(pacing_sec)
        for r in web_search(q, n=n, snippet_len=snippet_len, freshness=freshness):
            u = r.get("url") or ""
            if u and u in seen_urls:
                continue
            if u:
                seen_urls.add(u)
            out.append(r)
    return out

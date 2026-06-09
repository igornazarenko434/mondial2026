"""Web-search adapter for the news agent (Day 8).

Currently wires Brave Search API (https://api.search.brave.com). Free tier:
**2,000 queries/month, 1 query/second.** The wrapper is fully OPTIONAL — if
`BRAVE_SEARCH_API_KEY` isn't set, every call returns []. The news agent then
proceeds with API-Football only.

THREE-LAYER QUOTA PROTECTION (defends the free tier):
  1. Per-second rate limiter — config/observability.py PROVIDER_LIMITS
     gives brave_search rate=1/per=1 (= 1 req/sec, the published Brave limit).
     Enforced by obs.external_call via the token-bucket.
  2. Monthly hard budget — same config sets budget=2000 budget_period='month'.
     `_budget_clear()` checks before each HTTP and short-circuits when at
     `BRAVE_BUDGET_BRAKE_FRACTION` (0.95) of cap → leaves the last 5% for
     kickoff-day spikes.
  3. Daily soft cap — `BRAVE_DAILY_LIMIT` (default 80/day) prevents one
     runaway day from blowing the whole month. Counts rolling-24h calls.

Usage:
    from core.data.web_search import web_search, quota_status
    results = web_search("Norway vs France WC 2026 lineup 2026-06-26", n=5)
    # → [{"title": "...", "snippet": "...", "url": "...", "date": "2026-06-26"}]
    print(quota_status())  # {"month_used": 612, "month_budget": 2000,
                            #  "month_fraction": 0.306, "day_used": 31,
                            #  "day_limit": 80, "ok": True}

Returns at most `n` results, each ≤ ~250 chars of snippet to keep our LLM
context budget bounded (see config/news.py::SNIPPET_LEN).
"""
from __future__ import annotations
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any
import requests

from core.obs.logging import get_logger
from config.news import BRAVE_DAILY_LIMIT, BRAVE_BUDGET_BRAKE_FRACTION

log = get_logger("data.web_search")

BRAVE_API_BASE = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_FRESHNESS = "pw"      # "past week" — matches our NEWS_RECENCY_HOURS=48 ceiling


def available() -> bool:
    """True iff a Brave key is configured. Caller can short-circuit."""
    return bool(os.environ.get("BRAVE_SEARCH_API_KEY"))


def _day_count() -> int:
    """Brave-search calls in the last rolling 24h, from the cost ledger.
    Cheap query; called once per outbound call so the daily cap is real-time."""
    try:
        from core.obs.cost import ledger
        c = ledger().conn
        with ledger()._lock:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            n = c.execute("SELECT COUNT(*) FROM api_calls "
                           "WHERE provider='brave_search' AND ts>=?",
                           (cutoff,)).fetchone()[0]
        return int(n or 0)
    except Exception:
        return 0


def _budget_clear() -> tuple[bool, str]:
    """Multi-gate guard: short-circuit BEFORE the HTTP request if either the
    monthly fraction or the daily cap would be exceeded.

    Day-9.11: returns (ok, reason) where reason distinguishes the FOUR ways
    a Brave call gets blocked: 'no_key' / 'monthly_brake' / 'daily_cap' /
    'monthly_check_failed' / 'ok'. The news_agent reads this to put a
    SPECIFIC placeholder in the LLM context AND to stamp news_brave_gate
    on the card. Fails CLOSED on monthly_check exception (was: log.debug +
    proceed, which made a sick ledger silently burn budget)."""
    # Gate 0: key set?
    if not available():
        return False, "no_key"
    # Gate 1: monthly fraction
    try:
        from core.obs.cost import ledger
        from config.observability import PROVIDER_LIMITS
        budget = (PROVIDER_LIMITS.get("brave_search") or {}).get("budget")
        if budget:
            st = ledger().quota_status("brave_search")
            used = int(st.get("used") or 0)
            if used >= budget * BRAVE_BUDGET_BRAKE_FRACTION:
                log.warning("brave_search MONTHLY brake hit: %d/%d (%.0f%%) >= %.0f%% — skipping",
                            used, budget, 100 * used / budget,
                            100 * BRAVE_BUDGET_BRAKE_FRACTION)
                return False, "monthly_brake"
    except Exception as e:                                # noqa: BLE001
        log.warning("brave_search monthly check failed: %s — failing closed", e)
        return False, "monthly_check_failed"

    # Gate 2: daily soft cap (rolling 24h)
    if BRAVE_DAILY_LIMIT > 0:
        used_today = _day_count()
        if used_today >= BRAVE_DAILY_LIMIT:
            log.warning("brave_search DAILY cap hit: %d/%d in last 24h — skipping",
                        used_today, BRAVE_DAILY_LIMIT)
            return False, "daily_cap"
    return True, "ok"


def quota_status() -> dict:
    """One-shot summary for tools/dashboard and CLI checks. Safe to call any
    time — never touches Brave; only reads our local ledger."""
    out = {"month_used": 0, "month_budget": 2000, "month_fraction": 0.0,
            "day_used": 0, "day_limit": BRAVE_DAILY_LIMIT, "ok": True,
            "key_set": available()}
    try:
        from core.obs.cost import ledger
        from config.observability import PROVIDER_LIMITS
        st = ledger().quota_status("brave_search")
        out["month_used"] = int(st.get("used") or 0)
        out["month_budget"] = int(st.get("budget") or 2000)
        out["month_fraction"] = round(out["month_used"] / max(1, out["month_budget"]), 3)
        out["day_used"] = _day_count()
        ok, reason = _budget_clear()
        out["ok"] = ok
        out["reason"] = reason         # Day-9.11: distinguish blocker
    except Exception as e:                                # noqa: BLE001
        log.debug("quota_status failed: %s", e)
    return out


def _headers() -> dict | None:
    key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not key:
        return None
    return {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": key,
    }


_HTML_TAG = __import__("re").compile(r"<[^>]+>")
_HTML_ENT = {"&amp;": "&", "&lt;": "<", "&gt;": ">",
             "&quot;": '"', "&#39;": "'", "&nbsp;": " "}


def _trim(s: str, n: int) -> str:
    """Compact snippet — strip HTML markup + collapse whitespace + cap length.

    Day-9.19: Brave returns `description` with HTML markup like
    `<strong>Mexico (co-host)</strong>` — these tags pass through unparsed
    into the LLM context as ugly noise. We strip tags + decode the common
    HTML entities (&amp;, &lt;, etc.) at the source so the LLM sees clean
    prose only."""
    if not s:
        return s
    s = _HTML_TAG.sub("", s)
    for ent, ch in _HTML_ENT.items():
        s = s.replace(ent, ch)
    return " ".join(s.split())[:n]


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
    # Quota gates BEFORE the HTTP — see module docstring; this never blocks
    # delivery, just returns no web results so API-Football alone drives the LLM.
    ok, _reason = _budget_clear()
    if not ok:
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

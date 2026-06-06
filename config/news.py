"""News agent configuration — search window, budget, recency (see
docs/NEWS_AGENT_PLAYBOOK.md). All env-overridable."""
import os

# Windows in which the news agent searches; it does NOT search at/after T-7m (lock).
SEARCH_WINDOWS = ("T-24h", "T-60m", "T-15m")
PRIMARY_WINDOW = "T-60m"                                  # lineups land ~1h out
NEWS_MAX_QUERIES = int(os.environ.get("NEWS_MAX_QUERIES", "6"))
NEWS_RECENCY_HOURS = int(os.environ.get("NEWS_RECENCY_HOURS", "48"))
DELTA_CLAMP = float(os.environ.get("NEWS_DELTA_CLAMP", "0.6"))

# Day-8 additions — token-budget control on the gathered context that we
# pass to the LLM. Caps prevent a single long Brave snippet from blowing the
# per-call token budget.
SNIPPET_LEN       = int(os.environ.get("NEWS_SNIPPET_LEN",       "250"))
CONTEXT_MAX_CHARS = int(os.environ.get("NEWS_CONTEXT_MAX_CHARS", "1800"))
PER_QUERY_RESULTS = int(os.environ.get("NEWS_PER_QUERY_RESULTS", "3"))

# Per-window query counts — used by news_agent.search_queries(...)
QUERIES_PER_WINDOW = {
    "T-24h": int(os.environ.get("NEWS_QUERIES_T_24H", "3")),
    "T-60m": int(os.environ.get("NEWS_QUERIES_T_60M", "6")),
    "T-15m": int(os.environ.get("NEWS_QUERIES_T_15M", "2")),
}

# Brave Search free-tier protection (Day 8) — two safeguards layered on top
# of the monthly 2000-query budget already in config/observability.py.
#
# DAILY soft cap: even though the monthly budget is enforced, a runaway run
# (bad retry loop, debug session) could spend it in one day. This caps the
# per-rolling-24h call count so we always have at least N days worth of
# capacity left for the actual tournament. Configurable; 0 disables.
BRAVE_DAILY_LIMIT = int(os.environ.get("BRAVE_DAILY_LIMIT", "80"))
# Hard CIRCUIT BREAKER: stop calling Brave when monthly usage crosses this
# fraction of budget. Leaves a safety margin so a delivery-spike day at
# kickoff doesn't kill the rest of the tournament.
BRAVE_BUDGET_BRAKE_FRACTION = float(os.environ.get("BRAVE_BUDGET_BRAKE_FRACTION", "0.95"))


def should_search(window: str) -> bool:
    """True if the news agent should look for info in this window (never at T-7m)."""
    return window in SEARCH_WINDOWS

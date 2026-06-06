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


def should_search(window: str) -> bool:
    """True if the news agent should look for info in this window (never at T-7m)."""
    return window in SEARCH_WINDOWS

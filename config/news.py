"""News agent configuration — search window, budget, recency (see
docs/NEWS_AGENT_PLAYBOOK.md). All env-overridable."""
import os

# Windows in which the news agent searches; it does NOT search at/after T-7m (lock).
SEARCH_WINDOWS = ("T-24h", "T-60m", "T-15m")
PRIMARY_WINDOW = "T-60m"                                  # lineups land ~1h out
NEWS_MAX_QUERIES = int(os.environ.get("NEWS_MAX_QUERIES", "6"))
NEWS_RECENCY_HOURS = int(os.environ.get("NEWS_RECENCY_HOURS", "48"))
DELTA_CLAMP = float(os.environ.get("NEWS_DELTA_CLAMP", "0.6"))


def should_search(window: str) -> bool:
    """True if the news agent should look for info in this window (never at T-7m)."""
    return window in SEARCH_WINDOWS

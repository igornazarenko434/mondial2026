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
#
# Day-9.19 audit: the original limits (250 / 1800 / 3) were tuned for early
# prototyping. Modern LLM input budgets are 128K-1M tokens; 1800 chars ≈ 450
# tokens uses 0.05% of even Claude Haiku's 200K window. Bumping these gives
# the LLM richer context AT NO COST — Brave bills per QUERY not per result,
# so PER_QUERY_RESULTS is free; the extra input tokens to the LLM cost a
# fraction of a cent. See docs/NEWS_AGENT_PLAYBOOK.md for trade-off math.
SNIPPET_LEN       = int(os.environ.get("NEWS_SNIPPET_LEN",       "600"))
# Day-9.25: bumped 3000 → 12000. Gemini Flash supports 1M tokens (~4M chars);
# 12000 is 0.3% of that. Lets the LLM see more rich context at zero token
# cost. Original 3000 truncated ~25% of Korea's T-24h Brave results and
# ~17% of Mexico's — silently losing potentially useful signal.
CONTEXT_MAX_CHARS = int(os.environ.get("NEWS_CONTEXT_MAX_CHARS", "12000"))
# Day-9.25: bumped 5 → 8. Brave returns up to 20 per query and bills per
# query (not per result) so larger n is FREE. More raw material → more
# survivors after relevance ranking → better signal density.
PER_QUERY_RESULTS = int(os.environ.get("NEWS_PER_QUERY_RESULTS", "8"))
# How many UNIQUE Brave results from across all queries actually make it
# into the context block (before final char cap). Day-9.25 bumped 15 → 20
# to take advantage of the bigger context budget.
WEB_RESULTS_IN_CONTEXT = int(os.environ.get("NEWS_WEB_RESULTS_IN_CONTEXT", "20"))
# Day-9.25: top-K ranked articles get LONGER snippets so the most-relevant
# content keeps detail past the 600-char baseline. Mid-pack articles use
# the baseline; below-threshold get dropped first when the context cap
# bites.
TOP_K_LONG_SNIPPET = int(os.environ.get("NEWS_TOP_K_LONG_SNIPPET", "5"))
LONG_SNIPPET_LEN   = int(os.environ.get("NEWS_LONG_SNIPPET_LEN",   "1200"))

# Per-window query counts — used by news_agent.search_queries(...)
# Calibrated to fit in Brave's $5/mo = 1,000-request free credit:
#   T-24h:  3 × 104 matches = 312
#   T-60m:  4 × 104 matches = 416  (was 6 — dropped weather + the per-team
#                                    duplicate lineup queries; the joint
#                                    "Mexico South Africa lineup <date>"
#                                    query covers both teams)
#   T-15m:  2 × 104 matches but ~70% reuse the T-60m result via cache → ~60
#   Tournament total: ~790, leaves ~21% headroom under 1,000.
QUERIES_PER_WINDOW = {
    "T-24h": int(os.environ.get("NEWS_QUERIES_T_24H", "3")),
    "T-60m": int(os.environ.get("NEWS_QUERIES_T_60M", "4")),
    "T-15m": int(os.environ.get("NEWS_QUERIES_T_15M", "2")),
}

# T-15m can REUSE the T-60m news deltas (stored in predictions.payload_json)
# when the prior result was high-confidence and recent. Skips ALL Brave +
# LLM calls at T-15m for that match. Set 0 to disable reuse and force a
# fresh search every T-15m.
T15M_REUSE_AGE_MIN = int(os.environ.get("NEWS_T15M_REUSE_AGE_MIN", "75"))
T15M_REUSE_MIN_CONFIDENCE = os.environ.get("NEWS_T15M_REUSE_MIN_CONFIDENCE", "medium")

# Brave Search free-tier protection (Day 8) — layered on top of the monthly
# 1,000-request budget in config/observability.py.
#
# DAILY soft cap: even though monthly budget is enforced, a runaway run could
# burn it in one day. Per-rolling-24h cap keeps multiple days' worth available.
BRAVE_DAILY_LIMIT = int(os.environ.get("BRAVE_DAILY_LIMIT", "60"))
# Hard CIRCUIT BREAKER: stop calling Brave when monthly usage crosses this
# fraction. 0.90 leaves a 100-request buffer for retries near month-end so a
# delivery-spike day at kickoff doesn't tip into paid territory.
BRAVE_BUDGET_BRAKE_FRACTION = float(os.environ.get("BRAVE_BUDGET_BRAKE_FRACTION", "0.90"))


def should_search(window: str) -> bool:
    """True if the news agent should look for info in this window (never at T-7m)."""
    return window in SEARCH_WINDOWS

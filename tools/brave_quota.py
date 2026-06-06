"""Day-8 Brave Search quota CLI.

Brave's "Search" plan billing (as of 2026):
  - $5.00 per 1,000 requests = $0.005 per request
  - Free $5/month credit auto-applied (= 1,000 free requests)
  - Above 1,000/mo → out-of-pocket at $0.005/request

This tool reads our local cost ledger only — no Brave API calls — and shows
both the free-credit balance and what the next 100 calls would cost if we
go over.

    python -m tools.brave_quota
"""
from __future__ import annotations
from dotenv import load_dotenv
load_dotenv(".env")

from core.data.web_search import quota_status, available


def _bar(frac: float, width: int = 30) -> str:
    filled = max(0, min(width, int(round(frac * width))))
    return "█" * filled + "·" * (width - filled)


def main() -> int:
    q = quota_status()
    key = "✓ set" if available() else "✗ not set (web search disabled)"

    month_used = q["month_used"]
    month_budget = q["month_budget"]
    month_frac = q["month_fraction"]
    day_used = q["day_used"]
    day_limit = q["day_limit"]

    # Dollar accounting
    cost_so_far = month_used * 0.005          # what Brave charged us this month
    free_credit = 5.00
    paid = max(0.0, cost_so_far - free_credit)
    remaining_free = max(0, month_budget - month_used)

    print(f"Brave Search — Search plan ($5 / 1,000 requests; $5/mo free credit)")
    print(f"  key:           {key}")
    print(f"  ledger:        {month_used:>4d} requests this month")
    print(f"  free budget:   [{_bar(month_frac)}] {month_frac * 100:>5.1f}%  ({remaining_free} requests remaining)")
    print(f"  cost so far:   ${cost_so_far:>5.2f}   (out-of-pocket: ${paid:.2f})")
    print(f"  day (24h):     {day_used:>4d} / {day_limit}")
    print(f"  green-light:   {q['ok']}")
    if not q["ok"]:
        if day_limit > 0 and day_used >= day_limit:
            print(f"  → daily soft-cap reached; resets as the 24h window rolls forward")
        elif month_frac >= 0.90:
            print(f"  → monthly hard-brake hit at 90%; resets at the 1st of next month")
    elif month_frac > 0.70:
        next_cost = max(0.0, (month_used + 100) * 0.005 - free_credit)
        print(f"  → projected cost if you make 100 more requests: ${next_cost:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

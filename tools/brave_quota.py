"""Day-8 Brave Search quota CLI — no API calls, reads our local cost ledger only.

    python -m tools.brave_quota
"""
from __future__ import annotations
from dotenv import load_dotenv
load_dotenv(".env")

from core.data.web_search import quota_status, available


def main() -> int:
    q = quota_status()
    key = "✓ set" if available() else "✗ not set (web search disabled)"
    print(f"Brave Search quota status")
    print(f"  key:           {key}")
    print(f"  month used:    {q['month_used']:>5d} / {q['month_budget']} "
           f"({q['month_fraction'] * 100:.1f}%)")
    print(f"  day used (24h):{q['day_used']:>5d} / {q['day_limit']}")
    print(f"  green-light:   {q['ok']}")
    if not q["ok"]:
        if q["day_used"] >= q["day_limit"]:
            print(f"  → daily soft-cap reached; resets as 24h window rolls forward")
        elif q["month_fraction"] >= 0.95:
            print(f"  → monthly hard-brake hit at 95%; resets at the 1st of next month")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

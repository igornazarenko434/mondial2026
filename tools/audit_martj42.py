"""Day-9.23: martj42 CSV — freshness + WC2026 coverage audit.

Verifies the historical-results dataset we use for Dixon-Coles fitting is:

  1. Fresh — last row dated within the last 30 days (international fixtures
     fire frequently; if our CSV is months stale we'd be fitting with a
     missing recent friendly window).
  2. Comprehensive — every one of the 48 WC2026 teams has at least N rows
     (default 5) in the dataset. Fewer = DC fit will have weak strengths.
  3. Reasonable size — total row count is in the expected ballpark (~50k
     after a century of international football). A row count well below
     this means the CSV was truncated or the column mapping changed.

  PYTHONPATH=. .venv/bin/python tools/audit_martj42.py
  PYTHONPATH=. .venv/bin/python tools/audit_martj42.py --min-rows 10
  PYTHONPATH=. .venv/bin/python tools/audit_martj42.py --refresh   # force fresh fetch

Costs: 0 API credits (GitHub raw CSV). Disk cached 24h.
"""
from __future__ import annotations
import argparse
import csv
import os
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


EXPECTED_MIN_TOTAL_ROWS = 2_500      # 4-year window (HISTORY_WINDOW_YEARS=4) ≈ ~4k rows
EXPECTED_MAX_AGE_DAYS = 30           # 1 month staleness ceiling


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="audit_martj42")
    p.add_argument("--groups-csv", default="data/wc2026_groups.csv")
    p.add_argument("--min-rows", type=int, default=5,
                   help="Minimum martj42 rows per WC2026 team (default 5)")
    p.add_argument("--refresh", action="store_true",
                   help="Force re-download (skip 24h disk cache)")
    args = p.parse_args(argv)

    print()
    print(f"  ╔════════════════════════════════════════════════════════════╗")
    print(f"  ║  martj42 historical-results audit")
    print(f"  ╚════════════════════════════════════════════════════════════╝")
    print()

    from core.data.results_io import historical_results
    from core.data import teams

    print(f"  Loading CSV (24h disk cache unless --refresh)...")
    if args.refresh:
        # invalidate by passing ttl_hours=0
        rows = historical_results(ttl_hours=0)
    else:
        rows = historical_results()
    print(f"  Total rows: {len(rows):,}")

    # 1. Reasonable size
    print()
    print(f"  ── 1. Size check ──")
    size_ok = len(rows) >= EXPECTED_MIN_TOTAL_ROWS
    print(f"    Total: {len(rows):,}  expected ≥ {EXPECTED_MIN_TOTAL_ROWS:,}  "
          f"{'✓' if size_ok else '✗'}")

    # 2. Freshness — historical_results uses days_ago, not date
    print()
    print(f"  ── 2. Freshness ──")
    days = [r.get("days_ago") for r in rows if r.get("days_ago") is not None]
    fresh_ok = False
    if days:
        min_age = min(days)
        max_age = max(days)
        fresh_ok = min_age <= EXPECTED_MAX_AGE_DAYS
        flag = '✓' if fresh_ok else f'⚠ stale ({min_age}d > {EXPECTED_MAX_AGE_DAYS}d)'
        print(f"    Newest match: {min_age}d ago  {flag}")
        print(f"    Oldest match: {max_age}d ago  (window = "
              f"{max_age // 365} years)")
    else:
        print(f"    ✗ No days_ago metadata on any row")

    # 3. Per-team coverage for WC2026 nations
    print()
    print(f"  ── 3. WC2026 team coverage (≥ {args.min_rows} rows each) ──")
    roster = []
    with open(args.groups_csv) as f:
        for row in csv.DictReader(f):
            roster.append(row["team"])

    # historical_results returns rows keyed 'home' / 'away' (already normalized)
    team_counts = Counter()
    for r in rows:
        for side in (r.get("home"), r.get("away")):
            if side:
                team_counts[side] += 1

    # Per-team breakdown
    coverage_ok = 0
    coverage_warn = 0
    coverage_fail = 0
    missing = []
    sparse = []
    for team in roster:
        n = team_counts.get(team, 0)
        if n == 0:
            coverage_fail += 1
            missing.append(team)
        elif n < args.min_rows:
            coverage_warn += 1
            sparse.append(f"{team} ({n})")
        else:
            coverage_ok += 1
    print(f"    OK (≥{args.min_rows} rows):     {coverage_ok}/{len(roster)}")
    print(f"    Sparse (< {args.min_rows}):       {coverage_warn}")
    print(f"    MISSING (0 rows):     {coverage_fail}")
    if sparse:
        print(f"    Sparse list: {', '.join(sparse[:15])}"
              + ("..." if len(sparse) > 15 else ""))
    if missing:
        print(f"    Missing list: {', '.join(missing)}")

    # Recency check — how many recent matches do top + bottom teams have?
    print()
    print(f"  ── 4. Recency per team (last 365 days) ──")
    recent_counts = Counter()
    for r in rows:
        if (r.get("days_ago") or 999) > 365:
            continue
        for side in (r.get("home"), r.get("away")):
            if side:
                recent_counts[side] += 1
    no_recent = [t for t in roster if recent_counts.get(t, 0) == 0]
    print(f"    Teams with 0 matches in last 365d: {len(no_recent)}/{len(roster)}")
    if no_recent:
        print(f"      → {', '.join(no_recent)}")

    print()
    print(f"  ── Summary ──")
    if size_ok and fresh_ok and coverage_fail == 0:
        print(f"  ✓ martj42 dataset is fresh, comprehensive, and covers all 48 WC2026 teams.")
        return 0
    print(f"  ⚠ Issues detected:")
    if not size_ok:
        print(f"    • Size below expected — possible truncation or fetch error")
    if not fresh_ok:
        print(f"    • CSV stale by >30 days — run with --refresh; "
              f"check upstream github.com/martj42/international_results")
    if coverage_fail > 0:
        print(f"    • {coverage_fail} WC2026 team(s) have ZERO rows — Dixon-Coles "
              f"will have NO signal for them; consider adding to "
              f"teams.normalize() aliases.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

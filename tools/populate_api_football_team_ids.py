"""One-shot warm-up: resolve every WC2026 team to its api-football team_id
and save to store/api_football_team_ids.json.

WHY
===

Without this cache, every daemon match-window pass burns 2 api-football
credits per match just for team_id lookups (1 per team). The free tier is
100 credits/day; at peak (4 simultaneous matches × T-60m + T-15m × 2
team-id lookups) = 16 credits/day on team_ids ALONE — wastes nearly 20%
of quota on data that NEVER CHANGES (team ids are stable forever).

Run THIS script once after the api_football daily quota resets (midnight
UTC) and the daemon needs zero team-id calls from then on.

Cost: ≤ 48 credits one-shot (1 per team, sometimes 2 if multiple variants
need to be tried). Even with quota at 80% used, this still fits in the
remaining 20 credits.

Usage:
    sudo -u mondial bash -c '
      cd /home/mondial/mondial2026
      set -a && source .env && set +a
      PYTHONPATH=. .venv/bin/python tools/populate_api_football_team_ids.py
    '

Idempotent: if a team is already in the cache, the script skips the API
call. Re-runs only fill in what's missing.

Re-run with --force to refresh the entire cache from scratch.
"""
from __future__ import annotations
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="populate_api_football_team_ids")
    p.add_argument("--force", action="store_true",
                   help="Re-fetch even teams already in the cache")
    p.add_argument("--groups-csv", default="data/wc2026_groups.csv",
                   help="Source-of-truth roster of 48 teams")
    args = p.parse_args(argv)

    from core.data import api_football as af

    cache = af._load_team_id_cache()
    print(f"\n  Current cache size: {len(cache)}")

    teams = []
    with open(args.groups_csv) as f:
        for row in csv.DictReader(f):
            teams.append(row["team"])
    print(f"  Roster: {len(teams)} teams from {args.groups_csv}")
    print()

    found = 0
    skipped = 0
    failed: list[str] = []
    quota_blocked: list[str] = []
    for i, team in enumerate(teams, 1):
        if team in cache and not args.force:
            print(f"  {i:>2}/48  {team:<28} → {cache[team]:<6}  (cached)")
            skipped += 1
            continue
        # Day-9.20: distinguish "real not-found" from "quota exhausted mid-run".
        # If _budget_clear is False, the find_team_id calls will return None
        # but it's a QUOTA issue, not a name issue — flag separately so the
        # user knows to retry tomorrow instead of editing aliases.
        budget_ok = af._budget_clear()
        tid = af.find_team_id(team)
        if tid:
            print(f"  {i:>2}/48  {team:<28} → {tid:<6}  ✓ fetched")
            found += 1
        elif not budget_ok or not af._budget_clear():
            print(f"  {i:>2}/48  {team:<28} → ?       ⚠ deferred (quota exhausted)")
            quota_blocked.append(team)
        else:
            print(f"  {i:>2}/48  {team:<28} → ?       ✗ NOT FOUND in api-football")
            failed.append(team)

    print()
    print(f"  Summary: {found} fetched, {skipped} already cached, "
          f"{len(quota_blocked)} quota-deferred, {len(failed)} not resolvable")
    if quota_blocked:
        print()
        print(f"  ⚠ Quota-deferred: {quota_blocked}")
        print(f"    These teams' api-football lookups were blocked by the daily")
        print(f"    100-credit cap, NOT by a missing alias. Re-run this script")
        print(f"    after midnight UTC (quota resets ~03:00 IDT) and the {len(quota_blocked)}")
        print(f"    remaining team(s) will be fetched — costs only {len(quota_blocked)} credits")
        print(f"    since the {found + skipped} already-cached teams will be skipped.")
    if failed:
        print()
        print(f"  ✗ Could not resolve: {failed}")
        print(f"    These teams will use Brave-only news context (no api-football")
        print(f"    injuries). Consider adding aliases to _TEAM_NAME_VARIANTS in")
        print(f"    core/data/api_football.py if api-football names them differently.")

    print(f"\n  Cache file: {af._TEAM_ID_CACHE_PATH}")
    print(f"  Cache size now: {len(af._load_team_id_cache())} teams")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

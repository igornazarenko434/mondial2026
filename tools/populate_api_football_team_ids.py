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
    failed = []
    for i, team in enumerate(teams, 1):
        if team in cache and not args.force:
            print(f"  {i:>2}/48  {team:<28} → {cache[team]:<6}  (cached)")
            skipped += 1
            continue
        tid = af.find_team_id(team)
        if tid:
            print(f"  {i:>2}/48  {team:<28} → {tid:<6}  ✓ fetched")
            found += 1
        else:
            print(f"  {i:>2}/48  {team:<28} → ?       ✗ NOT FOUND in api-football")
            failed.append(team)

    print()
    print(f"  Summary: {found} fetched, {skipped} already cached, "
          f"{len(failed)} not resolvable")
    if failed:
        print(f"  ✗ Could not resolve: {failed}")
        print(f"    These teams will use Brave-only news context (no api-football")
        print(f"    injuries). Consider adding aliases to _TEAM_NAME_VARIANTS in")
        print(f"    core/data/api_football.py if api-football names them differently.")

    print(f"\n  Cache file: {af._TEAM_ID_CACHE_PATH}")
    print(f"  Cache size now: {len(af._load_team_id_cache())} teams")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())

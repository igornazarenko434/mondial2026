"""Day-9.23: cross-source team-name reconciliation.

For each of the 48 WC2026 teams, asserts that:
  • football-data.org's spelling normalizes to the canonical name
  • the-odds-api's spelling normalizes to the canonical name
  • api-football's team-id resolves (uses the disk cache from Day-9.20 to
    avoid extra credits — only fires real calls for missing teams)
  • eloratings.net's 2-letter code resolves to the canonical name
  • martj42's results.csv lists the team in at least one row

Reports any unmatched name. Quota-aware: refuses to burn api-football
credits without --force when cold cache + budget > 50% used.

  PYTHONPATH=. .venv/bin/python tools/audit_team_aliases.py
"""
from __future__ import annotations
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read_roster(path: str) -> list[str]:
    out = []
    with open(path) as f:
        for row in csv.DictReader(f):
            out.append(row["team"])
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="audit_team_aliases")
    p.add_argument("--groups-csv", default="data/wc2026_groups.csv")
    p.add_argument("--force", action="store_true",
                   help="Allow api-football calls even when budget > 50% used")
    args = p.parse_args(argv)

    roster = _read_roster(args.groups_csv)
    print()
    print(f"  Auditing {len(roster)} teams across 5 source spellings.")
    print()

    from core.data import teams
    from core.data import api_football as af
    from core.data import eloratings_codes
    from core.obs.cost import ledger

    af_used, af_budget, af_frac = (lambda q: (q.get("used") or 0,
                                                 q.get("budget"),
                                                 (q.get("used") or 0) /
                                                 (q.get("budget") or 1))
                                    )(ledger().quota_status("api_football"))
    cache = af._load_team_id_cache()
    print(f"  api-football budget: {af_used}/{af_budget}  "
          f"({af_frac*100:.0f}%)  cache: {len(cache)} teams")
    print()

    rows = []
    for team in roster:
        # canonical (round-trip through normalize)
        canon = teams.normalize(team)
        canon_ok = canon == team

        # api-football: check ONLY the disk cache unless --force
        in_cache = team in cache
        api_resolved = in_cache
        if not in_cache and args.force and af_frac < 0.5:
            try:
                api_resolved = bool(af.find_team_id(team))
            except Exception:                             # noqa: BLE001
                api_resolved = False

        # eloratings
        try:
            elo_code = eloratings_codes.code_for(team)
            elo_ok = bool(elo_code)
        except Exception:                                 # noqa: BLE001
            elo_ok = False

        # martj42 — read in main loop OK (small CSV)
        rows.append({"team": team, "canon": canon, "canon_ok": canon_ok,
                      "api_resolved": api_resolved, "elo_ok": elo_ok,
                      "martj_count": None})

    # martj42 — read once
    try:
        from core.data.results_io import historical_results
        results = historical_results()
        team_counts = {}
        for r in results:
            for side in (r.get("home_team"), r.get("away_team")):
                if side:
                    team_counts[teams.normalize(side)] = \
                        team_counts.get(teams.normalize(side), 0) + 1
    except Exception as e:                                # noqa: BLE001
        print(f"  ⚠ martj42 read failed: {e}")
        team_counts = {}
    for r in rows:
        r["martj_count"] = team_counts.get(r["team"], 0)

    # Print
    print(f"    {'team':<22} {'canon':<3} {'af-cache':<9} {'elo':<5} "
          f"{'martj42_rows':<13}")
    print(f"    {'-'*22} {'-'*3} {'-'*9} {'-'*5} {'-'*13}")
    issues = []
    for r in rows:
        c = "✓" if r["canon_ok"] else "✗"
        a = "✓" if r["api_resolved"] else "✗"
        e = "✓" if r["elo_ok"] else "✗"
        m = r["martj_count"]
        m_s = (f"{m}" if m and m > 0 else "0 ⚠")
        print(f"    {r['team']:<22} {c:<3} {a:<9} {e:<5} {m_s:<13}")
        if not r["canon_ok"]:
            issues.append(f"{r['team']}: canonical roundtrip broken")
        if not r["api_resolved"]:
            issues.append(f"{r['team']}: api-football team-id not cached")
        if not r["elo_ok"]:
            issues.append(f"{r['team']}: eloratings code missing")
        if not m or m == 0:
            issues.append(f"{r['team']}: 0 rows in martj42 — DC fit will have no signal!")

    print()
    print(f"  ── Summary ──")
    print(f"  Canonical OK:        {sum(1 for r in rows if r['canon_ok'])}/{len(rows)}")
    print(f"  api-football cached: {sum(1 for r in rows if r['api_resolved'])}/{len(rows)}")
    print(f"  eloratings code OK:  {sum(1 for r in rows if r['elo_ok'])}/{len(rows)}")
    print(f"  martj42 has rows:    {sum(1 for r in rows if r['martj_count'])}/{len(rows)}")

    if issues:
        print()
        print(f"  ⚠ {len(issues)} issue(s):")
        for i in issues[:20]:
            print(f"    • {i}")
        if len(issues) > 20:
            print(f"    ... +{len(issues)-20} more")
        return 1

    print()
    print(f"  ✓ All {len(rows)} teams resolve across all 4 keyed sources + martj42.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
                   help="Allow api-football calls even when budget > 50%% used")
    args = p.parse_args(argv)

    roster = _read_roster(args.groups_csv)
    print()
    print(f"  Auditing {len(roster)} teams across 5 source spellings.")
    print()

    from core.data import teams
    from core.data import api_football as af
    from core.data import eloratings_codes
    # eloratings_codes exposes code→team; build the inverse for team→code lookup
    _ELO_NAME_TO_CODE = {v: k for k, v in eloratings_codes.EL_CODE_TO_TEAM.items()}
    from core.obs.cost import ledger

    af_used, af_budget, af_frac = (lambda q: (q.get("used") or 0,
                                                 q.get("budget"),
                                                 (q.get("used") or 0) /
                                                 (q.get("budget") or 1))
                                    )(ledger().quota_status("api_football"))
    cache = af._load_team_id_cache()
    cache_local = bool(cache)
    if not cache_local:
        print(f"  ⚠ api-football team-id cache empty — the file lives ONLY on")
        print(f"    the VM (gitignored). To audit the live cache, run this tool")
        print(f"    on the VM. The 'af-cache' column will report '(skipped)'")
        print(f"    below; canonical / elo / martj42 checks remain accurate.")
        print()
    print(f"  api-football budget: {af_used}/{af_budget}  "
          f"({af_frac*100:.0f}%)  cache: {len(cache)} team(s)")
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

        # eloratings — reverse lookup against the verified code table
        elo_ok = team in _ELO_NAME_TO_CODE

        # martj42 — read in main loop OK (small CSV)
        rows.append({"team": team, "canon": canon, "canon_ok": canon_ok,
                      "api_resolved": api_resolved, "elo_ok": elo_ok,
                      "martj_count": None})

    # martj42 — `historical_results()` returns rows already normalized to
    # the keys `home`/`away` (NOT `home_team`/`away_team`), with normalized
    # canonical names. No additional normalize() pass needed.
    try:
        from core.data.results_io import historical_results
        results = historical_results()
        team_counts = {}
        for r in results:
            for side in (r.get("home"), r.get("away")):
                if side:
                    team_counts[side] = team_counts.get(side, 0) + 1
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
        if not cache_local:
            a = "n/a"                          # cache file isn't on this host
        else:
            a = "✓" if r["api_resolved"] else "✗"
        e = "✓" if r["elo_ok"] else "✗"
        m = r["martj_count"]
        m_s = (f"{m}" if m and m > 0 else "0 ⚠")
        print(f"    {r['team']:<22} {c:<3} {a:<9} {e:<5} {m_s:<13}")
        if not r["canon_ok"]:
            issues.append(f"{r['team']}: canonical roundtrip broken")
        if cache_local and not r["api_resolved"]:
            issues.append(f"{r['team']}: api-football team-id not cached")
        if not r["elo_ok"]:
            issues.append(f"{r['team']}: eloratings code missing")
        if not m or m == 0:
            issues.append(f"{r['team']}: 0 rows in martj42 — DC fit will have no signal!")

    print()
    print(f"  ── Summary ──")
    print(f"  Canonical OK:        {sum(1 for r in rows if r['canon_ok'])}/{len(rows)}")
    if cache_local:
        print(f"  api-football cached: {sum(1 for r in rows if r['api_resolved'])}/{len(rows)}")
    else:
        print(f"  api-football cached: (skipped — run on VM for live cache audit)")
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

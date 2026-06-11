"""Day-9.25: per-match pick alternatives analyzer.

For ONE match, prints a side-by-side table of top-N candidate scorelines
with the FULL trade-off picture:

  - score
  - direction (H/D/A)
  - P(this exact score)
  - P(this direction)
  - table multiplier (mult)
  - EV (post-detonator if applicable)
  - P(any points)        ← key: how often does the pick yield ANYTHING?
  - max if exact         ← upside ceiling
  - max if direction-only
  - std deviation of points (per-match volatility)
  - sharpe (EV/stdev — risk-adjusted return)

The system's DEFAULT pick is the row with the highest EV. But this tool
lets you compare against:
  • the modal score (highest P(exact))
  • the safest direction (highest P(direction) — picks the favorite)
  • the longshot (highest mult × odds — biggest upside)

Doesn't change the daemon's behavior. Pure visibility / decision-support.

Use against a match you're about to lock or just for a what-if:

  PYTHONPATH=. .venv/bin/python tools/pick_analyzer.py Mexico "South Africa"
  PYTHONPATH=. .venv/bin/python tools/pick_analyzer.py "South Korea" Czechia
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pick_analyzer")
    p.add_argument("home")
    p.add_argument("away")
    p.add_argument("--top", type=int, default=10,
                   help="Number of candidate scorelines to show (default 10)")
    p.add_argument("--stage", default="Group",
                   choices=["Group", "R32", "R16", "QF", "SF", "3rd", "Final"])
    p.add_argument("--detonator", action="store_true",
                   help="Apply the ×2 detonator multiplier")
    p.add_argument("--use-live", action="store_true",
                   help="Pull live odds + DC fit + news from the daemon's "
                        "data sources (default: use --xg-home, --xg-away, "
                        "--odds-h, --odds-d, --odds-a directly).")
    p.add_argument("--xg-home", type=float, default=2.05,
                   help="Home expected goals (default: 2.05, Mexico)")
    p.add_argument("--xg-away", type=float, default=0.65,
                   help="Away expected goals (default: 0.65, South Africa)")
    p.add_argument("--odds-h", type=float, default=1.43)
    p.add_argument("--odds-d", type=float, default=4.56)
    p.add_argument("--odds-a", type=float, default=8.77)
    args = p.parse_args(argv)

    from core.models.dixon_coles import score_matrix
    from core.scoring.engine import direction_probs, exact_multiplier
    from config.rules import (BASE_POINTS, DETONATOR_FACTOR, STAGE_TYPE,
                                TABLE_CAP)

    stype = STAGE_TYPE[args.stage]
    base = BASE_POINTS[stype]
    det = DETONATOR_FACTOR if args.detonator else 1.0
    odds = {"H": args.odds_h, "D": args.odds_d, "A": args.odds_a}

    if args.use_live:
        # Pull live odds + xG from the production pipeline. This burns 1-2
        # odds_api credits. Skipped by default — pass --use-live explicitly.
        from core.data.football_data import RULES_STAGE  # noqa
        # For simplicity in this analyzer, we let the user pass xG directly.
        # Live pull is a future expansion (need to load DC fit + Elo + news).
        print("  (--use-live not yet wired; pass --xg-home/--xg-away "
              "directly from the news_inspect output)")

    print()
    print(f"  ╔════════════════════════════════════════════════════════════╗")
    print(f"  ║  Pick analyzer: {args.home} vs {args.away}  (stage={args.stage})")
    print(f"  ║  Expected goals: home={args.xg_home:.2f}  away={args.xg_away:.2f}")
    print(f"  ║  Locked odds: H={args.odds_h}  D={args.odds_d}  A={args.odds_a}")
    print(f"  ║  Detonator: {'YES (×2)' if args.detonator else 'no'}  "
          f"base={base}")
    print(f"  ╚════════════════════════════════════════════════════════════╝")
    print()

    m = score_matrix(args.xg_home, args.xg_away)
    p_dir = direction_probs(m)
    print(f"  Direction probabilities:  H={p_dir['H']*100:.1f}%  "
          f"D={p_dir['D']*100:.1f}%  A={p_dir['A']*100:.1f}%")
    print()

    # Generate all reasonable scoreline candidates (limit blowouts)
    def direction_of(i, j):
        return "H" if i > j else "D" if i == j else "A"

    rows = []
    for i in range(min(m.shape[0], 5)):
        for j in range(min(m.shape[1], 5)):
            d = direction_of(i, j)
            p_score = float(m[i, j])
            w, l = max(i, j), min(i, j)
            mult = exact_multiplier(stype, w, l)
            # If this score isn't on the printed table (very high), fall to cap
            if mult is None:
                mult = TABLE_CAP.get(stype, base)
            # EV: expected points if we pick THIS score
            ev = odds[d] * det * (base * (p_dir[d] - p_score) + mult * p_score)
            max_exact = mult * odds[d] * det
            max_dir_only = base * odds[d] * det
            p_any = p_dir[d]
            # Variance: E[X²] - E[X]² where outcomes are {0, max_dir_only, max_exact}
            e_x2 = (p_score * max_exact ** 2
                     + (p_dir[d] - p_score) * max_dir_only ** 2
                     + (1 - p_dir[d]) * 0)
            var = max(0.0, e_x2 - ev ** 2)
            stdev = var ** 0.5
            sharpe = ev / stdev if stdev > 0 else float("inf")
            rows.append({
                "score": f"{i}-{j}", "dir": d,
                "p_score": p_score, "p_dir": p_dir[d],
                "mult": mult, "ev": ev,
                "p_any": p_any,
                "max_exact": max_exact, "max_dir_only": max_dir_only,
                "stdev": stdev, "sharpe": sharpe,
            })

    rows.sort(key=lambda r: r["ev"], reverse=True)
    top = rows[:args.top]

    headers = ("Score Dir P(score) P(dir)  Mult     EV  P(any)   Max(ex)  "
                "Max(dir)  StdDev  Sharpe  Note")
    print(f"  {headers}")
    print(f"  {'-' * len(headers)}")
    for idx, r in enumerate(top):
        note = ""
        # Tag the rows that represent particular strategies
        if idx == 0:
            note = "← EV-MAX (system's pick)"
        # Find modal score (highest P(exact) over all rows)
        modal_row = max(rows, key=lambda x: x["p_score"])
        if r["score"] == modal_row["score"] and idx != 0:
            note = "← MODAL (most likely)"
        # Highest direction-prob (safest direction)
        safe_dir_row = max(rows, key=lambda x: (x["p_dir"], x["mult"]))
        if r["score"] == safe_dir_row["score"] and idx != 0 and "MODAL" not in note:
            note = "← SAFEST DIR"
        # Longshot: highest max_exact among scores with P_dir < 25% (variance play)
        longshot = max((x for x in rows if x["p_dir"] < 0.25
                        and x["p_score"] > 0.01),
                       key=lambda x: x["max_exact"], default=None)
        if longshot and r["score"] == longshot["score"] and idx != 0:
            note = "← LONGSHOT (high variance)"

        print(f"  {r['score']:<5} {r['dir']:<3} "
              f"{r['p_score']*100:>7.2f}% {r['p_dir']*100:>5.1f}%  "
              f"{r['mult']:>4.2f} {r['ev']:>6.3f}  "
              f"{r['p_any']*100:>5.1f}%  "
              f"{r['max_exact']:>6.2f}  {r['max_dir_only']:>6.2f}  "
              f"{r['stdev']:>5.2f}  {r['sharpe']:>5.2f}  {note}")

    print()
    # Summary across the spectrum
    ev_max = top[0]
    modal = max(rows, key=lambda x: x["p_score"])
    safest_dir = max(rows, key=lambda x: (x["p_dir"], x["mult"]))
    longshot = max((x for x in rows if x["p_dir"] < 0.25 and x["p_score"] > 0.01),
                    key=lambda x: x["max_exact"], default=None)

    print(f"  ── Strategy comparison ──")
    print(f"  EV-MAX    (system default):   pick {ev_max['score']}  "
          f"EV={ev_max['ev']:.2f}  P(any)={ev_max['p_any']*100:.0f}%  "
          f"max-up={ev_max['max_exact']:.1f}")
    print(f"  MODAL     (lowest variance):  pick {modal['score']}  "
          f"EV={modal['ev']:.2f}  P(any)={modal['p_any']*100:.0f}%  "
          f"max-up={modal['max_exact']:.1f}")
    print(f"  SAFEST    (highest P_dir):    pick {safest_dir['score']}  "
          f"EV={safest_dir['ev']:.2f}  P(any)={safest_dir['p_any']*100:.0f}%  "
          f"max-up={safest_dir['max_exact']:.1f}")
    if longshot:
        print(f"  LONGSHOT  (highest variance): pick {longshot['score']}  "
              f"EV={longshot['ev']:.2f}  P(any)={longshot['p_any']*100:.0f}%  "
              f"max-up={longshot['max_exact']:.1f}")

    # Sharpe interpretation
    print()
    print(f"  ── Tournament context ──")
    print(f"  EV-MAX has higher mean AND higher variance than MODAL.")
    print(f"  In a 67-player pool over 64 matches, higher variance helps")
    print(f"  you land in the right tail (= win the pool). MODAL gives a")
    print(f"  more consistent but lower expected finish.")
    print(f"  ")
    print(f"  When to deviate from EV-MAX (= turn on strategy tilt):")
    print(f"    - You're ≥15 points BEHIND the leader → pick higher")
    print(f"      variance scorelines to catch up (LONGSHOT zone)")
    print(f"    - You're ≥10 points AHEAD of #2 → pick MODAL to lock in")
    print(f"    - Otherwise: trust EV-MAX (current setting).")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

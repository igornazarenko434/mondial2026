"""Pool-wide picks inspector — read-only.

Two sections:

  1. BROAD BETS — every player's futures picks (winner / cinderella /
     goldenBoot / bestPlayer) sorted by displayName.

  2. FIRST MATCH PICKS — every player who's submitted a 1X2 + exact-score
     pick for the chronologically-first SCHEDULED match in the tournament
     (defaults to Mexico v South Africa, the opener).

Pure read, never writes anything (no NEGEV_ALLOW_WRITES gate touched).
Two Negev API calls total (broadBets + match-details).

Usage:
    PYTHONPATH=. .venv/bin/python tools/show_pool_picks.py
    PYTHONPATH=. .venv/bin/python tools/show_pool_picks.py --match-id <mid>
    PYTHONPATH=. .venv/bin/python tools/show_pool_picks.py --home Mexico --away "South Africa"
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="show_pool_picks")
    p.add_argument("--match-id", default=None,
                   help="Negev match_id (overrides --home/--away)")
    p.add_argument("--home", default="Mexico",
                   help="Home team for the match-picks section")
    p.add_argument("--away", default="South Africa",
                   help="Away team for the match-picks section")
    p.add_argument("--skip-broad", action="store_true",
                   help="Skip the broad-bets section")
    p.add_argument("--skip-match", action="store_true",
                   help="Skip the per-match picks section")
    args = p.parse_args(argv)

    try:
        from integrations import negev_toto_mcp as ntm
    except Exception as e:                                # noqa: BLE001
        print(f"FAILED to import Negev MCP: {e}", file=sys.stderr)
        return 2

    tid = os.environ.get("NEGEV_TOURNAMENT_ID", "?")
    me = os.environ.get("MY_PARTICIPANT", "Igor")
    friends = [x.strip() for x in
                os.environ.get("FRIEND_PARTICIPANTS", "").split(",")
                if x.strip()]
    tracked = (me, *friends)

    print()
    print(f"  ╔════════════════════════════════════════════════════════════╗")
    print(f"  ║  Pool picks — tournament {tid}")
    print(f"  ║  Tracked: {me} (you) + friends: {friends or '(none)'}")
    print(f"  ╚════════════════════════════════════════════════════════════╝")

    # ──────────────── 1. BROAD BETS ────────────────
    if not args.skip_broad:
        print()
        print("  ── 1. Broad bets (futures) — everyone who submitted ──────────")
        try:
            rows = ntm.toto_get_broad_bets()
        except Exception as e:                            # noqa: BLE001
            print(f"  ✗ toto_get_broad_bets failed: {e}")
            rows = []

        # Only rows with at least one selection (signals an actual submission)
        submitted = [r for r in rows
                      if r.get("winner") or r.get("cinderella")
                         or r.get("goldenBoot") or r.get("bestPlayer")]
        empty = [r for r in rows if r not in submitted]

        print(f"  Submitted: {len(submitted)} player(s)   "
              f"Empty (not yet locked): {len(empty)}")
        print()
        print(f"    {'displayName':<22} {'Winner':<16} {'Cinderella':<16} "
              f"{'GoldenBoot':<22} {'BestPlayer':<22}")
        print(f"    {'-'*22} {'-'*16} {'-'*16} {'-'*22} {'-'*22}")
        for r in submitted:
            name = r.get("displayName") or "?"
            tag = (" ← you" if name == me
                    else (" ← tracked" if name in tracked else ""))
            w = (r.get("winner") or "-")[:16]
            ci = (r.get("cinderella") or "-")[:16]
            gb = (r.get("goldenBoot") or "-")[:22]
            bp = (r.get("bestPlayer") or "-")[:22]
            # bestPlayer values often carry a "roster_" prefix in storage
            if isinstance(bp, str) and bp.startswith("roster_"):
                bp = bp[len("roster_"):]
            print(f"    {name:<22} {w:<16} {ci:<16} {gb:<22} {bp:<22}{tag}")

        if empty:
            print()
            print(f"  Not yet submitted ({len(empty)}): "
                  + ", ".join((r.get("displayName") or "?") for r in empty[:30])
                  + ("..." if len(empty) > 30 else ""))

    # ──────────────── 2. FIRST MATCH — per-player picks ────────────────
    if not args.skip_match:
        print()
        if args.match_id:
            print(f"  ── 2. Match picks — match_id={args.match_id} ────────────────")
            details = ntm.toto_get_match_details(match_id=args.match_id)
        else:
            print(f"  ── 2. Match picks — {args.home} vs {args.away} (opener) ────")
            details = ntm.toto_get_match_details(home=args.home, away=args.away)

        if "error" in (details or {}):
            print(f"  ✗ {details['error']}")
        else:
            m = details.get("match") or {}
            picks = details.get("friendsPicks") or []
            grid_name = details.get("exactPtsGridName") or "?"
            print(f"  Match: {m.get('home', '?')} vs {m.get('away', '?')}   "
                  f"status={m.get('status', '?')}   "
                  f"stage={m.get('stage', '?')}   grid={grid_name}")
            print(f"  Picks recorded: {len(picks)} player(s)")
            print()
            print(f"    {'displayName':<22} {'Score':<14} {'Mult':<6} "
                  f"{'Points':<8} {'advancesTeam':<14}")
            print(f"    {'-'*22} {'-'*14} {'-'*6} {'-'*8} {'-'*14}")
            # Sort: tracked first, then by points desc, then by name
            def sort_key(p):
                name = p.get("displayName") or ""
                is_tracked = name in tracked
                return (0 if is_tracked else 1,
                        -(p.get("points") or 0),
                        name)
            for pi in sorted(picks, key=sort_key):
                name = pi.get("displayName") or "?"
                h, a = pi.get("homeScore"), pi.get("awayScore")
                score = f"{h} — {a}" if h is not None else "(none)"
                br = pi.get("breakdown") or {}
                mult = br.get("multiplier") or br.get("detonatorMultiplier")
                mult_s = f"×{mult}" if mult else "?"
                pts = pi.get("points")
                pts_s = f"{pts:.1f}" if isinstance(pts, (int, float)) else "?"
                adv = pi.get("advancesTeam") or ""
                tag = (" ← you" if name == me
                        else (" ← tracked" if name in tracked else ""))
                print(f"    {name:<22} {score:<14} {mult_s:<6} {pts_s:<8} "
                      f"{adv:<14}{tag}")

            # Quick aggregation: pick popularity (collapsed by exact score)
            from collections import Counter
            score_counts = Counter()
            dir_counts = Counter()
            for pi in picks:
                h, a = pi.get("homeScore"), pi.get("awayScore")
                if h is None:
                    continue
                score_counts[(h, a)] += 1
                dir_counts["H" if h > a else "D" if h == a else "A"] += 1
            if score_counts:
                print()
                print(f"  Most popular exact scores:")
                for (h, a), c in score_counts.most_common(8):
                    pct = c / len(picks) * 100
                    print(f"    {h} — {a}    {c} pick(s)  ({pct:.0f}%)")
                print()
                print(f"  Direction split: "
                      f"H={dir_counts['H']} ({dir_counts['H']/len(picks)*100:.0f}%)  "
                      f"D={dir_counts['D']} ({dir_counts['D']/len(picks)*100:.0f}%)  "
                      f"A={dir_counts['A']} ({dir_counts['A']/len(picks)*100:.0f}%)")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Print my current Negev standings rank — pasteable single-script verifier.

No multi-line python -c required (which hits SyntaxError when shell escape
characters get mangled across nested quoting). Same pattern as
`tools/show_my_broad_bets.py`.

Usage:
    sudo -u mondial bash -c '
      cd /home/mondial/mondial2026
      set -a && source .env && set +a
      PYTHONPATH=. .venv/bin/python tools/show_my_rank.py
    '

Output:
    Tournament:    n40ykJlOIA9Mg839hz91  (Negev Toto 2026)
    My displayName: Igor

    Rank (default — matches app):    56 / 67   (bots included with 0 pts)
    Rank (strategy / bots filtered):  53 / 64   (human competitors only)

    Around me (top → bottom):
      52  Eindar
      53  Igor   ← you (strategy view)
      54  Shelach
      55  Alpert
      56  Igor   ← you (app view)
      57  Shelach
      …
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str] | None = None) -> int:
    me_name = os.environ.get("MY_PARTICIPANT", "Igor")
    tid = os.environ.get("NEGEV_TOURNAMENT_ID", "?")

    try:
        from integrations import negev_toto_mcp as ntm
    except Exception as e:                              # noqa: BLE001
        print(f"FAILED to import Negev MCP: {e}", file=sys.stderr)
        return 2

    try:
        rows_app = ntm.toto_get_standings(include_bots=True)
        rows_strategy = ntm.toto_get_standings(include_bots=False)
    except Exception as e:                              # noqa: BLE001
        print(f"FAILED to read standings from Negev: {e}", file=sys.stderr)
        return 2

    me_app = next((r for r in rows_app if r.get("player") == me_name), None)
    me_strat = next((r for r in rows_strategy if r.get("player") == me_name), None)

    print()
    print(f"  Tournament:     {tid}")
    print(f"  My displayName: {me_name}")
    print()
    if me_app:
        print(f"  Rank (default — matches app):     {me_app['rank']:>3} / {len(rows_app)}   (bots included with 0 pts)")
    else:
        print(f"  ✗ {me_name!r} NOT FOUND in include_bots=True roster")
    if me_strat:
        print(f"  Rank (strategy / bots filtered):  {me_strat['rank']:>3} / {len(rows_strategy)}   (human competitors only)")
    else:
        print(f"  ✗ {me_name!r} NOT FOUND in include_bots=False roster")
    print()

    if me_app:
        # Show ±2 around me in the app view
        my_rank = me_app["rank"]
        print(f"  Around me in the APP-equivalent view (rank ±2):")
        for r in rows_app:
            if abs(r["rank"] - my_rank) <= 2:
                marker = "  ← YOU" if r["player"] == me_name else ""
                role = r.get("role") or ""
                role_badge = f" [{role}]" if role and role != "player" else ""
                print(f"    {r['rank']:>3}  {r['player']:<22} pts={r['total']:>5}{role_badge}{marker}")
    print()

    # Leader / gap — bot-filtered so it's vs a real competitor
    if rows_strategy and me_strat:
        leader = rows_strategy[0]
        gap = leader["total"] - me_strat["total"]
        print(f"  Real-competitor leader: {leader['player']!r} with {leader['total']:.1f} pts")
        print(f"  My gap to leader: {gap:.1f}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

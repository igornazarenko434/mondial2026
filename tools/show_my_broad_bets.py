"""Print MY current broad-bet selections from Negev, with display-name resolution.

One-shot read-back tool — no writes, no network beyond the necessary reads.
Built to dodge the IndentationError class of bugs caused by pasting multi-
line `python -c "..."` into chat-formatted bash blocks.

Usage:
    sudo -u mondial bash -c '
      cd /home/mondial/mondial2026
      set -a && source .env && set +a
      PYTHONPATH=. .venv/bin/python tools/show_my_broad_bets.py
    '

Output:
    user        Igor   (uid nsauuOzpJ...)
    tournament  Negev Toto 2026  (n40ykJlOIA9Mg839hz91)
    updatedAt   2026-06-07T18:33:33.515237+00:00
    winner       team_Portugal            → Portugal
    cinderella   team_Uzbekistan          → Uzbekistan
    goldenBoot   1780696074187            → Mbappe
    bestPlayer   roster_q2HO78NvnRUsmiTJ… → Arkadi   ✓ roster_ prefix
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resolve(cat_id: str, opt_id: str | None, categories: dict) -> str:
    """Reverse-lookup the human name for one saved option id. Returns the
    name + an optional badge for known-prefix sanity checks."""
    if opt_id is None:
        return "(none)"
    cats = (categories or {}).get("categories") or []
    target = next((c for c in cats if c.get("id") == cat_id), None)
    if not target:
        return f"{opt_id} (category not found)"
    options = target.get("options") or []
    o = next((o for o in options if o.get("id") == opt_id), None)
    if not o:
        return f"{opt_id} (no option matches — saved id not in current category)"
    name = o.get("name", "?")
    # Day-9.11.d cross-check: bestPlayer ids MUST carry roster_
    if cat_id == "bestPlayer":
        return f"{name}   {'✓ roster_ prefix' if opt_id.startswith('roster_') else '✗ MISSING roster_ prefix'}"
    return name


def main(argv: list[str] | None = None) -> int:
    me_name = os.environ.get("MY_PARTICIPANT", "Igor")

    try:
        from integrations import negev_toto_mcp as ntm
    except Exception as e:                              # noqa: BLE001
        print(f"FAILED to import Negev MCP: {e}", file=sys.stderr)
        return 2

    try:
        rows = ntm.toto_get_broad_bets()
    except Exception as e:                              # noqa: BLE001
        print(f"FAILED to read broadBets from Negev: {e}", file=sys.stderr)
        return 2

    me = next((r for r in rows if r.get("displayName") == me_name), None)
    if me is None:
        print(f"NOT FOUND — no broadBets row for displayName={me_name!r}", file=sys.stderr)
        print(f"  (visible rows: {[r.get('displayName') for r in rows]})", file=sys.stderr)
        return 1

    try:
        cats = ntm.toto_get_broad_bet_categories()
    except Exception as e:                              # noqa: BLE001
        print(f"WARNING: could not read categories for name resolution: {e}",
              file=sys.stderr)
        cats = {"categories": []}

    print()
    print(f"  user        {me.get('displayName')}   (uid {(me.get('userId') or '')[:12]}…)")
    print(f"  tournament  {os.environ.get('NEGEV_TOURNAMENT_ID', '?')}")
    print(f"  updatedAt   {me.get('updatedAt')}")
    print()
    print(f"  {'category':<12} {'saved id':<28} → resolved name")
    print(f"  {'-' * 12} {'-' * 28}   {'-' * 25}")
    for cat in ("winner", "cinderella", "goldenBoot", "bestPlayer"):
        val = me.get(cat)
        name = _resolve(cat, val, cats)
        val_s = (str(val) if val is not None else "(none)")
        if len(val_s) > 26:
            val_s = val_s[:24] + "…"
        print(f"  {cat:<12} {val_s:<28} → {name}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

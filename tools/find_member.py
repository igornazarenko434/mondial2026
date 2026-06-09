"""Find Negev Toto members by substring — case-insensitive across displayName,
uid, and role. Returns the exact strings to paste into .env as
MY_PARTICIPANT / FRIEND_PARTICIPANTS values.

Why this exists: typing the WRONG displayName into config silently breaks
every downstream lookup (sync_negev_standings, daily_summary, strategy).
The Negev app sometimes shows nicknames vs the underlying displayName field;
this tool resolves the ambiguity by printing what's actually in Firestore.

Usage:
    sudo -u mondial bash -c '
      cd /home/mondial/mondial2026
      set -a && source .env && set +a
      PYTHONPATH=. .venv/bin/python tools/find_member.py vaadia
    '

Multiple matches → pick the right one from the printed list.
Zero matches → the name is wrong OR the user hasn't joined this tournament.
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="find_member")
    p.add_argument("query", help="Substring to search (case-insensitive). "
                                   "Matched against displayName, uid, and role.")
    p.add_argument("--include-bots", action="store_true",
                   help="Also show bot accounts (default: skip)")
    p.add_argument("--all-users", action="store_true",
                   help="Search ALL Negev users globally, not just this "
                        "tournament's roster (useful if the friend hasn't "
                        "joined our tournament yet).")
    args = p.parse_args(argv)

    try:
        from integrations import negev_toto_mcp as ntm
    except Exception as e:                                # noqa: BLE001
        print(f"FAILED to import Negev MCP: {e}", file=sys.stderr)
        return 2

    q = args.query.strip().lower()
    if not q:
        print("ERROR: empty query", file=sys.stderr)
        return 2

    tid = os.environ.get("NEGEV_TOURNAMENT_ID", "?")
    print()
    print(f"  Searching for: {args.query!r}  (case-insensitive substring)")
    print(f"  Scope: {'ALL Negev users' if args.all_users else f'tournament {tid}'}")
    print(f"  Bots: {'included' if args.include_bots else 'excluded'}")
    print()

    if args.all_users:
        # Direct read of the users collection — finds Negev members who
        # haven't joined our tournament yet (rare but possible).
        try:
            users = ntm._read_all("users")               # type: ignore[attr-defined]
        except Exception as e:                            # noqa: BLE001
            print(f"FAILED to read users collection: {e}", file=sys.stderr)
            return 2
        candidates = []
        for u in users:
            if not args.include_bots and (u.get("role") == "bot"):
                continue
            disp = (u.get("displayName") or "").lower()
            uid = (u.get("uid") or "").lower()
            role = (u.get("role") or "").lower()
            if q in disp or q in uid or q in role:
                candidates.append({
                    "player": u.get("displayName") or u.get("uid") or "?",
                    "uid": u.get("uid"),
                    "role": u.get("role") or "",
                    "total": float(u.get("pointsTotal") or 0),
                    "in_our_tournament": tid in (u.get("tournaments") or []),
                })
    else:
        try:
            rows = ntm.toto_get_standings(include_bots=args.include_bots)
        except Exception as e:                            # noqa: BLE001
            print(f"FAILED to read standings: {e}", file=sys.stderr)
            return 2
        candidates = []
        for r in rows:
            disp = (r.get("player") or "").lower()
            uid = (r.get("uid") or "").lower()
            role = (r.get("role") or "").lower()
            if q in disp or q in uid or q in role:
                candidates.append({**r, "in_our_tournament": True})

    if not candidates:
        print(f"  ✗ No matches for {args.query!r}.")
        print(f"    Try --all-users if the person hasn't joined this tournament,")
        print(f"    or --include-bots if you're looking for a bot account.")
        return 1

    print(f"  ✓ Found {len(candidates)} match(es):")
    print()
    for i, c in enumerate(candidates, 1):
        joined = "" if c.get("in_our_tournament", True) else "  ⚠ NOT in our tournament"
        rank = f"  rank {c['rank']:>3}" if "rank" in c else ""
        role = f"  [{c['role']}]" if c.get("role") and c["role"] != "player" else ""
        print(f"    {i}. displayName = {c['player']!r}")
        print(f"        uid        = {c.get('uid', '?')}")
        print(f"        total      = {c.get('total', 0):.1f} pts{rank}{role}{joined}")
        print()

    if len(candidates) == 1:
        c = candidates[0]
        print(f"  → To track this person, add to .env:")
        print(f"      FRIEND_PARTICIPANTS={c['player']}")
        print(f"    (or extend an existing list:  FRIEND_PARTICIPANTS=Alon,{c['player']})")
    else:
        print(f"  → Multiple matches. Pick the correct displayName from above")
        print(f"    and paste it into FRIEND_PARTICIPANTS in .env.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

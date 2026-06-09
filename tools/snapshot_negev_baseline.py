"""Capture every user's pre-tournament pointsTotal so post-game we can
compute WC2026-specific contribution = current - baseline.

THE PROBLEM
-----------
The user-doc field `pointsTotal` (and `directionPoints` / `broadBetPoints`
/ `exactScoreCount`) is GLOBAL across every tournament a user has joined.
Pre-tournament that's invisible (everyone at 0 except the 3 bots who carry
~4.3 pts of residue from old tournaments). Post-game these fields grow as
Negev's server-side Cloud Function scores WC2026 bets — but the residue
stays mixed in. The Negev web app shows tournament-specific points
(verified live: pre-tournament Chinchilla displays at 0 pts, not 4.3) via
a path we can't read (403 on subcollections).

THE SAFETY NET
--------------
Take a snapshot of every member's current `pointsTotal` etc. RIGHT NOW
(before kickoff), persist it to a JSON file. After games start, anywhere
we display "WC2026 score" we compute:

    contribution = max(0, current - baseline)

For humans: baseline ≈ 0, so contribution == current  (already works)
For bots:   baseline = ~4.3 → contribution = current - 4.3 = WC2026 only
For NEW members joining mid-tournament: baseline = their pointsTotal at
    first observation; they get 0 contribution until they place a bet.

This script just CAPTURES the snapshot — it does not change any live
behaviour. The baseline file is then read by sync_negev_standings post-
game (a follow-up wire-up if/when the bot divergence becomes visible).

Usage:
    sudo -u mondial bash -c '
      cd /home/mondial/mondial2026
      set -a && source .env && set +a
      PYTHONPATH=. .venv/bin/python tools/snapshot_negev_baseline.py
    '

Output:
    store/negev_baseline_<tournament_id>.json  (gitignored via store/*.json)

The file is timestamped + immutable. Re-running OVERWRITES with the
current state — only run BEFORE games start (Thu 11 Jun 22:00 IDT for
WC2026 opener). After that, the existing file is the source of truth.

Idempotency: refuses to overwrite if any user already has non-zero
pointsTotal that wasn't there at the previous snapshot (a sign games
have started and the previous baseline is the right one to keep).
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _baseline_path(tid: str) -> str:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "store", f"negev_baseline_{tid}.json")


def main(argv: list[str] | None = None) -> int:
    from integrations import negev_toto_mcp as ntm

    tid = os.environ.get("NEGEV_TOURNAMENT_ID", "").strip()
    if not tid:
        print("ERROR: NEGEV_TOURNAMENT_ID not set", file=sys.stderr)
        return 2

    path = _baseline_path(tid)

    # Load existing if present — for idempotency / safety check
    existing = None
    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = json.load(f)
        except Exception as e:                          # noqa: BLE001
            print(f"WARNING: existing baseline unreadable ({e}); will overwrite")

    # Pull current state from Negev
    try:
        users = ntm._read_all("users")
    except Exception as e:                              # noqa: BLE001
        print(f"FAILED to read users from Negev: {e}", file=sys.stderr)
        return 2

    in_tournament = [u for u in users if tid in (u.get("tournaments") or [])]
    snapshot = {
        "tournament_id": tid,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "n_members": len(in_tournament),
        "users": {},
    }
    for u in in_tournament:
        uid = u.get("uid")
        if not uid:
            continue
        snapshot["users"][uid] = {
            "displayName": u.get("displayName"),
            "role": u.get("role"),
            "pointsTotal": float(u.get("pointsTotal") or 0),
            "directionPoints": float(u.get("directionPoints") or 0),
            "broadBetPoints": float(u.get("broadBetPoints") or 0),
            "exactScoreCount": int(u.get("exactScoreCount") or 0),
        }

    # Safety check vs existing snapshot
    if existing:
        # If any user's pointsTotal has INCREASED since the existing snapshot,
        # games may have started — refuse to overwrite the better baseline.
        warnings = []
        for uid, cur in snapshot["users"].items():
            old = existing.get("users", {}).get(uid)
            if old and cur["pointsTotal"] > old["pointsTotal"]:
                warnings.append(
                    f"{cur['displayName']!r}: {old['pointsTotal']} → "
                    f"{cur['pointsTotal']} (+{cur['pointsTotal'] - old['pointsTotal']:.1f})")
        if warnings:
            print("⚠ pointsTotal has INCREASED since previous baseline — "
                  "games may have started. Refusing to overwrite the previous "
                  "baseline (which is the correct pre-tournament reference).")
            print("Increased users:")
            for w in warnings[:10]:
                print(f"  {w}")
            print(f"\nExisting baseline preserved at: {path}")
            return 1

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

    bot_residue = [(u["displayName"], u["pointsTotal"])
                   for u in snapshot["users"].values()
                   if u["role"] == "bot" and u["pointsTotal"] > 0]
    nonzero_humans = [(u["displayName"], u["pointsTotal"])
                       for u in snapshot["users"].values()
                       if u["role"] != "bot" and u["pointsTotal"] > 0]

    print(f"\n  ✓ Baseline captured at {snapshot['captured_at']}")
    print(f"  ✓ {len(in_tournament)} users snapshotted → {path}")
    print()
    print(f"  Bot residue (these counts will be SUBTRACTED from future displays):")
    for name, pts in bot_residue:
        print(f"    {name:<20} pts={pts}")
    if nonzero_humans:
        print()
        print(f"  Humans with non-zero baseline (unexpected pre-tournament):")
        for name, pts in nonzero_humans:
            print(f"    {name:<20} pts={pts}")
    else:
        print()
        print(f"  ✓ All humans baseline=0 — no residue to subtract")
    return 0


if __name__ == "__main__":
    sys.exit(main())

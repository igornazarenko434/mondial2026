"""Inspect one match's current state across all three sources:

  1. Negev — who has actually submitted a pick for this match (with name + score)
  2. the-odds-api — live decimal odds RIGHT NOW (sharpest available book)
  3. Our local DB — most-recent snapshotted odds + persisted card for this match

Use it to verify "what is reality, RIGHT NOW" before kickoff: did Vaadia pick?
What does Pinnacle say at this moment? What did we lock at T-7m? Costs 1
Negev call + 1 odds_api call (1 credit out of 500/mo).

Usage (on the VM):
    sudo -u mondial bash -c '
      cd /home/mondial/mondial2026
      set -a && source .env && set +a
      PYTHONPATH=. .venv/bin/python tools/match_status.py "Mexico" "South Africa"
    '
"""
from __future__ import annotations
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="match_status")
    p.add_argument("home")
    p.add_argument("away")
    p.add_argument("--no-live-odds", action="store_true",
                   help="Skip the the-odds-api call (use cached DB snapshot only)")
    args = p.parse_args(argv)

    print()
    print(f"  ╔════════════════════════════════════════════════════════════╗")
    print(f"  ║  Match: {args.home} vs {args.away}")
    print(f"  ║  Now (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  ╚════════════════════════════════════════════════════════════╝")

    # ──────────────── 1. Negev: who has picked? ────────────────
    print()
    print("  ── 1. Negev current state ────────────────────────────────────")
    try:
        from integrations import negev_toto_mcp as ntm
        details = ntm.toto_get_match_details(home=args.home, away=args.away)
        if "error" in (details or {}):
            print(f"  ⚠ {details['error']}")
        else:
            m = details.get("match") or {}
            my = details.get("myPrediction")
            picks = details.get("friendsPicks") or []
            mult = details.get("bingoMultiplier")
            grid_name = details.get("exactPtsGridName") or "?"

            print(f"  Match status: {m.get('status', '?')}  "
                  f"KO {m.get('date', '?')}  stage={m.get('stage', '?')}  "
                  f"(grid: {grid_name})")
            if my:
                print(f"  YOUR pick:   {args.home} {my.get('home')} — "
                      f"{args.away} {my.get('away')}"
                      + (f"   (mult ×{mult})" if mult else ""))
            else:
                print(f"  YOUR pick:   (not submitted yet)")

            tracked = (os.environ.get("MY_PARTICIPANT", "Igor"),
                       *(os.environ.get("FRIEND_PARTICIPANTS", "")
                         .split(",") if os.environ.get("FRIEND_PARTICIPANTS")
                         else ()))
            tracked = tuple(t.strip() for t in tracked if t.strip())

            print()
            print(f"  All picks recorded so far: {len(picks)} player(s)")
            by_name = {pi.get("displayName"): pi for pi in picks}
            for name in tracked:
                pi = by_name.get(name)
                marker = " ← you" if name == tracked[0] else "  ← tracked"
                if pi:
                    h, a = pi.get("homeScore"), pi.get("awayScore")
                    pts = pi.get("points")
                    pts_tag = f"  ({pts:.2f} pts)" if isinstance(pts, (int, float)) else ""
                    print(f"    ✓ {name:<18} {args.home} {h} — {args.away} {a}"
                          f"{pts_tag}{marker}")
                else:
                    print(f"    ✗ {name:<18} (no pick yet){marker}")

            # Anyone else (untracked) who's picked
            others = [pi for pi in picks if pi.get("displayName") not in tracked]
            if others:
                print(f"\n  Other players picked ({len(others)}):  "
                      + ", ".join(pi.get("displayName", "?") for pi in others[:10])
                      + ("..." if len(others) > 10 else ""))
    except Exception as e:                                # noqa: BLE001
        print(f"  ✗ Negev fetch failed: {e}")

    # ──────────────── 2. the-odds-api: LIVE odds right now ────────────────
    print()
    print("  ── 2. Live market odds (the-odds-api) ────────────────────────")
    if args.no_live_odds:
        print("  (skipped, --no-live-odds)")
    else:
        try:
            from core.data.oddsapi import fetch_match_odds, DEFAULT_PREFER_BOOKS
            from core.obs.cost import ledger
            q = ledger().quota_status("odds_api")
            print(f"  Budget: {q.get('used', 0)}/{q.get('budget', 500)} credits used "
                  f"({q.get('fraction', 0)*100:.1f}%)  (costs 2 credits per call)")
            odds = fetch_match_odds(args.home, args.away)
            if not odds:
                print(f"  ⚠ No odds returned (over budget, no event match, or "
                      f"no preferred book).")
            else:
                print(f"  Source book: {odds.get('book')}  "
                      f"(chain tried: {' → '.join(DEFAULT_PREFER_BOOKS)} → consensus)")
                H, D, A = odds.get("H"), odds.get("D"), odds.get("A")
                print(f"  Decimal odds: {args.home} {H}  /  Draw {D}  /  {args.away} {A}")
                # Devig to show implied probabilities — sanity check
                try:
                    from core.data.oddsapi import devig
                    p = devig({"H": H, "D": D, "A": A})
                    print(f"  Implied prob (devigged): "
                          f"{args.home} {p['H']*100:.1f}%  /  "
                          f"Draw {p['D']*100:.1f}%  /  "
                          f"{args.away} {p['A']*100:.1f}%")
                except Exception as e:                    # noqa: BLE001
                    print(f"  (devig failed: {e})")
        except Exception as e:                            # noqa: BLE001
            print(f"  ✗ odds_api fetch failed: {e}")

    # ──────────────── 3. Our last persisted snapshot ────────────────
    print()
    print("  ── 3. Last cached snapshot (what the next card would use) ───")
    try:
        from store.db import connect
        conn = connect()
        # match_id lookup by team-pair
        row = conn.execute(
            "SELECT match_id, utc_kickoff, status, home_goals, away_goals "
            "FROM matches WHERE home=? AND away=?",
            (args.home, args.away)).fetchone()
        if not row:
            print(f"  ⚠ Match not in local matches table.")
        else:
            mid, ko, status, hg, ag = row
            print(f"  Local match_id={mid}  ko={ko}  status={status}"
                  + (f"  score={hg}-{ag}" if hg is not None else ""))
            snaps = conn.execute(
                "SELECT captured_at, book, odds_h, odds_d, odds_a "
                "FROM odds_snapshots WHERE match_id=? "
                "ORDER BY rowid DESC LIMIT 5", (mid,)).fetchall()
            if not snaps:
                print(f"  No odds_snapshots rows for this match yet.")
            else:
                print(f"  Last {len(snaps)} snapshot(s):")
                for w, book, h, d, a in snaps:
                    print(f"    {w:<6} ({book:<14}): {args.home} {h}  /  "
                          f"Draw {d}  /  {args.away} {a}")
            # Most-recent persisted card for this match
            pred = conn.execute(
                "SELECT created_at, window, pick_dir, pick_h, pick_a, "
                "expected_points FROM predictions WHERE match_id=? "
                "ORDER BY created_at DESC LIMIT 1", (mid,)).fetchone()
            if pred:
                created, w, pdir, ph, pa, ep = pred
                print(f"\n  Last persisted card ({w}, {created}):")
                print(f"    pick={pdir} {args.home} {ph} — {args.away} {pa}  "
                      f"EV={ep}")
    except Exception as e:                                # noqa: BLE001
        print(f"  ✗ DB inspect failed: {e}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

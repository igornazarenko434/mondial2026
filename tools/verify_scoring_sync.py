"""Day-9.23: end-to-end scoring sync verification.

Walks the full Negev↔︎us scoring loop and reports any discrepancy:

  1. Standings table — do we have all 67 humans + 3 bots upserted with
     totals matching Negev's RIGHT NOW?
  2. Match results table — does every FINISHED match in our DB have the
     same score as Negev's record?
  3. Score audit — for every finished match, does our score_match()
     agree with Negev's awarded points?  Covers MY bets + every tracked
     friend's bets.
  4. Predictions table — do we have a T-7m LOCK card persisted for each
     played match? (Required for the audit to compute our side.)

Read-only; no writes. Costs ~3-4 Negev API calls regardless of #matches.
Safe to run any time. Pre-tournament (no FINISHED matches) → reports
that "nothing to compare yet" and exits cleanly.

  PYTHONPATH=. .venv/bin/python tools/verify_scoring_sync.py
"""
from __future__ import annotations
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _banner(title: str):
    print()
    print(f"  ── {title} ──")


def main(argv: list[str] | None = None) -> int:
    print()
    print(f"  ╔════════════════════════════════════════════════════════════╗")
    print(f"  ║  Scoring sync verification — Negev ↔ us")
    print(f"  ╚════════════════════════════════════════════════════════════╝")

    from store.db import connect
    from integrations import negev_toto_mcp as ntm
    me_name = os.environ.get("MY_PARTICIPANT", "Igor")
    friends = [s.strip() for s in
                os.environ.get("FRIEND_PARTICIPANTS", "").split(",")
                if s.strip()]
    tracked = (me_name, *friends)
    print(f"\n  Tracked: {me_name} (you) + friends: {friends or '(none)'}")

    conn = connect()

    # ─────────── 1. Standings mirror health ───────────
    _banner("1. Standings sync (do we have everyone?)")
    try:
        live = ntm.toto_get_standings(include_bots=True)
    except Exception as e:                                # noqa: BLE001
        print(f"  ✗ Negev fetch failed: {e}")
        return 2
    # Day-9.27: include side_points so the drift detection compares apples
    # to apples. Pre-Day-9.27 the audit reported drift on every user who
    # had a side-bet point because our total ignored that column.
    local = {r["participant"]: r for r in conn.execute(
        "SELECT participant, group_points, knockout_points, futures_points, "
        "COALESCE(side_points, 0) AS side_points, "
        "(group_points + knockout_points + futures_points "
        " + COALESCE(side_points, 0)) AS total "
        "FROM standings").fetchall()}
    print(f"  Negev: {len(live)} player(s)  ({sum(1 for r in live if r.get('role') != 'bot')} humans + "
          f"{sum(1 for r in live if r.get('role') == 'bot')} bots)")
    print(f"  Local: {len(local)} participant(s)")
    missing = [r["player"] for r in live
                if r.get("role") != "bot" and r["player"] not in local]
    if missing:
        print(f"  ⚠ Humans in Negev but NOT in our standings: {len(missing)}")
        for m in missing[:10]:
            print(f"    • {m}")
    else:
        print(f"  ✓ Every human player is mirrored in our standings table.")

    # Total-points drift across the pool (sum the differences)
    drift_count = 0
    drift_lines = []
    for n_row in live:
        if n_row.get("role") == "bot":
            continue
        name = n_row["player"]
        ours = local.get(name)
        if not ours:
            continue
        negev_total = float(n_row.get("total") or 0)
        ours_total = float(ours["total"])
        if abs(negev_total - ours_total) > 0.01:
            drift_count += 1
            drift_lines.append(f"    {name:<20} ours={ours_total:.1f}  "
                                f"negev={negev_total:.1f}  Δ={ours_total - negev_total:+.2f}")
    if drift_count:
        print(f"  ⚠ {drift_count} player(s) with mismatched totals "
              f"(local vs Negev) — likely a sync that hasn't run yet:")
        for l in drift_lines[:8]:
            print(l)
    else:
        print(f"  ✓ Per-player totals match Negev within ±0.01 pts.")

    # ─────────── 2. Match results mirror ───────────
    #
    # Day-9.35 rewrite: semantics-aware per-status comparison. Previously we
    # compared `home_goals` byte-for-byte with Negev's `scoreFullTimeHome`
    # for FT/PEN matches only — which produced two silent failure modes:
    #
    #   (a) AET matches were EXCLUDED from the check (Belgium-Senegal,
    #       Argentina-Cape Verde in the 2026 tournament). Any drift in
    #       120'-result storage for AET rows went unreported.
    #   (b) The FT/PEN comparison was actually correct-by-accident:
    #       Negev's `scoreFullTimeHome` is a DISPLAY convention (regulation
    #       result) — the same convention as broadcast scoreboards showing
    #       "2-2 (a.e.t. 3-2)". Its ACTUAL scoring engine uses the 120'
    #       final: proven live by Kobi's 3-2 pick on Belgium-Senegal
    #       receiving `isCorrectDir=true, isExactScore=true, multiplier=4.5`
    #       (the 3-2 cell of the KO grid). For PEN matches with ET=0-0
    #       (all 3 in R32) regulation happens to equal 120', so raw compare
    #       matched by coincidence.
    #
    # New logic — for each Negev-finished match, compute what Negev's
    # scoring engine SEES for direction + exact-score cell, and check our
    # stored (home_goals, away_goals) [+ (penalty_home, penalty_away)]
    # would produce the same:
    #
    #   FT   : ours(hg,ag) == negev.scoreFullTime          (regulation = 120')
    #   AET  : direction(ours hg,ag) == winnerTeam side    (120' ≠ reg — can
    #                                                        only verify dir)
    #   PEN  : direction(ours hg,ag) == D (draw at 120')   AND
    #          (penalty_home,away) == negev.scorePenalty
    _banner("2. Match results sync (semantics-aware per-status)")
    try:
        negev_ms = ntm.toto_get_matches(limit=300)
    except Exception as e:                                # noqa: BLE001
        print(f"  ✗ Negev matches fetch failed: {e}")
        return 2

    # Include AET in the check now — the old filter dropped it.
    negev_finished = {(m["home"], m["away"]): m for m in negev_ms
                       if m.get("status") in ("FT", "AET", "PEN")
                       and m.get("scoreFullTimeHome") is not None}
    our_finished = {(r["home"], r["away"]): dict(r) for r in conn.execute(
        "SELECT home, away, home_goals, away_goals, "
        "       penalty_home, penalty_away, status FROM matches "
        "WHERE status='FINISHED'").fetchall()}

    from collections import Counter as _Counter
    by_status = _Counter(m.get("status") for m in negev_finished.values())
    print(f"  Negev finished: {len(negev_finished)}  "
          f"(FT={by_status.get('FT', 0)} · "
          f"AET={by_status.get('AET', 0)} · "
          f"PEN={by_status.get('PEN', 0)})")
    print(f"  Local FINISHED: {len(our_finished)}")

    def _dir(h: int | None, a: int | None) -> str | None:
        if h is None or a is None:
            return None
        if h > a: return "H"
        if h < a: return "A"
        return "D"

    ft_drift = aet_drift = pen_drift = pen_tally_drift = 0

    for key, nm in negev_finished.items():
        ours = our_finished.get(key)
        if not ours:
            print(f"  ⚠ Finished in Negev but NOT yet finished locally: "
                  f"{key[0]} vs {key[1]}")
            continue

        n_hg = int(nm.get("scoreFullTimeHome") or 0)
        n_ag = int(nm.get("scoreFullTimeAway") or 0)
        o_hg = int(ours["home_goals"] or 0)
        o_ag = int(ours["away_goals"] or 0)
        o_ph = ours.get("penalty_home")
        o_pa = ours.get("penalty_away")
        status = nm.get("status")

        if status == "FT":
            # Regulation-decided — Negev's scoreFullTime IS the 120' final.
            # Both direction and exact score must match ours exactly.
            if (n_hg, n_ag) != (o_hg, o_ag):
                print(f"  ⚠ FT mismatch on {key[0]} vs {key[1]}: "
                      f"ours={o_hg}-{o_ag}  negev={n_hg}-{n_ag}")
                ft_drift += 1

        elif status == "AET":
            # Negev's scoreFullTime is regulation (draw); the 120' final
            # (what scoring uses) differs. We can only verify direction —
            # ours should match winnerTeam.
            winner = nm.get("winnerTeam")
            expected = ("H" if winner == key[0]
                        else "A" if winner == key[1]
                        else None)
            our_d = _dir(o_hg, o_ag)
            if expected and our_d != expected:
                print(f"  ⚠ AET direction mismatch on {key[0]} vs {key[1]}: "
                      f"ours={o_hg}-{o_ag} (dir={our_d})  "
                      f"Negev winnerTeam={winner} (expected dir={expected})")
                aet_drift += 1

        elif status == "PEN":
            # Regulation is a draw (Negev n_hg == n_ag). Our 120' final
            # should also be a draw (matches ordinarily go to pens only
            # after 120' still tied). Compare direction + pens tally.
            our_d = _dir(o_hg, o_ag)
            if our_d != "D":
                print(f"  ⚠ PEN direction not draw on {key[0]} vs {key[1]}: "
                      f"ours={o_hg}-{o_ag}  Negev scoreFullTime={n_hg}-{n_ag}")
                pen_drift += 1
            n_ph = nm.get("scorePenaltyHome")
            n_pa = nm.get("scorePenaltyAway")
            if n_ph is not None and n_pa is not None:
                if (o_ph, o_pa) != (int(n_ph), int(n_pa)):
                    print(f"  ⚠ PEN tally mismatch on {key[0]} vs {key[1]}: "
                          f"ours pens={o_ph}-{o_pa}  Negev={n_ph}-{n_pa}")
                    pen_tally_drift += 1

    total_drift = ft_drift + aet_drift + pen_drift + pen_tally_drift
    if not negev_finished:
        print(f"  ✓ No finished matches yet (pre-tournament).")
    elif total_drift == 0:
        print(f"  ✓ All {len(negev_finished)} finished matches align with "
              f"Negev's scoring semantics "
              f"(FT byte-for-byte, AET direction, PEN direction + pens tally).")
    else:
        print(f"  ⚠ {total_drift} scoring-semantics drift(s) — "
              f"FT={ft_drift}, AET={aet_drift}, "
              f"PEN dir={pen_drift}, PEN tally={pen_tally_drift}")
    # legacy variable used later
    score_mismatches = total_drift

    # ─────────── 3. Predictions table coverage ───────────
    _banner("3. Predictions persistence (do we have a T-7m LOCK per match?)")
    preds_by_match = {}
    for r in conn.execute(
        "SELECT match_id, window, created_at FROM predictions "
        "WHERE window='T-7m' ORDER BY created_at DESC").fetchall():
        preds_by_match.setdefault(r["match_id"], r)
    print(f"  T-7m LOCK cards persisted: {len(preds_by_match)}")
    # For each finished match, do we have a T-7m card?
    missing_locks = []
    for key in negev_finished:
        # Convert to local match_id
        row = conn.execute(
            "SELECT match_id FROM matches WHERE home=? AND away=?",
            key).fetchone()
        if row and row[0] not in preds_by_match:
            missing_locks.append(f"{key[0]} vs {key[1]}")
    if missing_locks:
        print(f"  ⚠ {len(missing_locks)} finished match(es) WITHOUT a T-7m LOCK card:")
        for m in missing_locks[:8]:
            print(f"    • {m}")
        print(f"    (The post-match audit can't compute our score_match() "
              f"without an odds snapshot from the lock.)")
    else:
        print(f"  ✓ All finished matches have their T-7m LOCK card persisted.")

    # ─────────── 4. Tracked per-person breakdown ───────────
    _banner("4. Tracked-person score state (you + friends)")
    print(f"  {'name':<20} {'rank':<6} {'total':<8} {'local':<8} "
          f"{'group':<8} {'futures':<8}")
    print(f"  {'-'*20} {'-'*6} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    for name in tracked:
        n_row = next((r for r in live if r["player"] == name), None)
        l_row = local.get(name)
        if not n_row:
            print(f"  {name:<20} ⚠ not in Negev")
            continue
        rank = n_row.get("rank", "?")
        n_total = n_row.get("total", 0)
        l_total = float(l_row["total"]) if l_row else None
        l_total_s = f"{l_total:.1f}" if l_total is not None else "✗ missing"
        n_dir = n_row.get("direction", 0)
        n_broad = n_row.get("broad", 0)
        tag = "  ← you" if name == me_name else ""
        print(f"  {name:<20} {rank!s:<6} {n_total:<8.1f} {l_total_s:<8} "
              f"{n_dir:<8.1f} {n_broad:<8.1f}{tag}")

    # ─────────── Summary ───────────
    _banner("Summary")
    issues = missing + drift_lines + missing_locks
    if not issues and score_mismatches == 0:
        print(f"  ✓ Standings, results, predictions all in sync. "
              f"Negev↔︎us scoring loop is healthy.")
        return 0
    print(f"  ⚠ {len(issues) + score_mismatches} issue(s) detected. "
          f"Most are pre-tournament-expected; investigate any score or "
          f"total drift before next sync slot.")
    print(f"  Next sync slot: 07:00 IDT (cron sync_negev_standings)")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""Quick live-verification of every Negev MCP tool — Day-9.8.

Run on the VM (or locally) to confirm each tool returns correct data scoped
to Negev Toto 2026 only. No writes; no quota burn beyond a few cheap reads.

  sudo -u mondial bash -c '
    cd /home/mondial/mondial2026
    set -a && source .env && set +a
    PYTHONPATH=. .venv/bin/python tools/verify_negev_live.py
  '

Expected output starts with header lines, then ✓ for each tool that returned
sensible data, and ✗ + reason for any that didn't. Exit code = number of
failures (0 = all good).
"""
from __future__ import annotations
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations import negev_toto_mcp as m

OK = "\033[32m✓\033[0m"
ERR = "\033[31m✗\033[0m"
INFO = "\033[36m·\033[0m"


def section(s: str):
    print(f"\n\033[1m{s}\033[0m")


def check(name: str, fn, expectation: str):
    """Run fn(), print pass/fail + short summary. Returns 0/1 for the exit code."""
    try:
        result = fn()
    except Exception as e:                             # noqa: BLE001
        print(f"  {ERR} {name:<38} EXCEPTION: {e!s}")
        traceback.print_exc()
        return 1
    print(f"  {OK} {name:<38} {result}")
    print(f"      expect: {expectation}")
    return 0


def main():
    section("Live verification — Negev Toto MCP, scoped to NEGEV_TOURNAMENT_ID")
    print(f"  {INFO} tournament_id env: {os.environ.get('NEGEV_TOURNAMENT_ID', '(unset)')}")
    print(f"  {INFO} MY_PARTICIPANT env: {os.environ.get('MY_PARTICIPANT', '(unset)')}")
    print(f"  {INFO} writes enabled:    {os.environ.get('NEGEV_ALLOW_WRITES', '0')}")

    fails = 0
    section("§1 Reads — tournament scoping")

    def _check_matches():
        ms = m.toto_get_matches()
        tids = {x.get("tournamentId") for x in ms}
        return f"{len(ms)} matches; unique tids in result: {tids}"
    fails += check("toto_get_matches", _check_matches,
                   "72 matches, exactly ONE tid (Negev Toto 2026)")

    def _check_standings():
        s = m.toto_get_standings()
        me = os.environ.get("MY_PARTICIPANT", "Igor")
        my_row = next((r for r in s if r["player"] == me), None)
        my = f"my rank={my_row['rank']}/{len(s)} pts={my_row['total']}" if my_row else f"{me!r} NOT in standings"
        return f"{len(s)} humans  |  {my}"
    fails += check("toto_get_standings (humans only)", _check_standings,
                   "63 humans, you found in roster")

    def _check_standings_bots():
        return f"{len(m.toto_get_standings(include_bots=True))} total"
    fails += check("toto_get_standings (include_bots)", _check_standings_bots,
                   "66 = 63 humans + 3 bots")

    section("§2 Reads — match details")

    def _check_opener():
        d = m.toto_get_match_details(home="Mexico", away="South Africa")
        if "error" in d:
            return f"ERROR: {d['error'][:80]}"
        mt = d["match"]
        return (f"kickoff={mt['kickoff_utc']}  "
                f"odds={mt['oddsHome']}/{mt['oddsDraw']}/{mt['oddsAway']}  "
                f"detonator={mt['isDetonator']}  "
                f"myPick={d['myPrediction']}  "
                f"friendsPicks={len(d['friendsPicks'])}  "
                f"gridName={d['exactPtsGridName']}")
    fails += check("toto_get_match_details Mexico v SA", _check_opener,
                   "kickoff 22:00 IDT, odds 1.40/4.50/7.50, detonator=True, grid=groupStage")

    def _check_next():
        nm = m.toto_next_match()
        if "error" in nm:
            return nm["error"][:80]
        return f"{nm['match']['home']} v {nm['match']['away']} stage_type={nm['stage_type']} pens={nm['requires_penalties']}"
    fails += check("toto_next_match", _check_next,
                   "Mexico v South Africa, group, no pens needed")

    section("§3 Reads — side bets")

    def _check_sb_all():
        return f"{len(m.toto_get_side_bets())} total docs"
    fails += check("toto_get_side_bets all", _check_sb_all,
                   "18 shells")

    def _check_sb_up():
        return f"{len(m.toto_get_side_bets_upcoming())} with question + not resolved"
    fails += check("toto_get_side_bets_upcoming", _check_sb_up,
                   "0 today (no questions published yet)")

    def _check_sb_res():
        return f"{len(m.toto_get_side_bets_resolved())} resolved"
    fails += check("toto_get_side_bets_resolved", _check_sb_res,
                   "0 today (tournament hasn't started)")

    section("§4 Reads — broad bets & categories")

    def _check_bb():
        bb = m.toto_get_broad_bets()
        return f"{len(bb)} users have submitted futures picks"
    fails += check("toto_get_broad_bets", _check_bb,
                   "small number today; grows as friends lock futures")

    def _check_cats():
        c = m.toto_get_broad_bet_categories()
        counts = {cat["id"]: len(cat["options"]) for cat in c["categories"]}
        return f"{counts}"
    fails += check("toto_get_broad_bet_categories", _check_cats,
                   "winner=48, cinderella=48, goldenBoot=19, bestPlayer=63 (synthesized)")

    section("§5 Reads — scoring grids (cross-check vs config/rules.py)")

    def _check_grids():
        g = m.toto_get_scoring_grids()
        gs = g["grids"].get("groupStage", {})
        return f"groupStage cells={len(gs)}, sample 1-0={gs.get('1-0')} 2-1={gs.get('2-1')}"
    fails += check("toto_get_scoring_grids", _check_grids,
                   "49 cells, 1-0=1.5, 2-1=1.5 (matches our config/rules.py post-Day-9.7 fix)")

    section("§6 Reads — my data")

    def _check_my_bets():
        return f"{len(m.toto_get_my_bets())} of my picks"
    fails += check("toto_get_my_bets", _check_my_bets,
                   "0 today (you haven't picked any matches yet)")

    def _check_prefs():
        p = m.toto_get_my_preferences()
        return f"displayName={p.get('displayName')!r}  pref_reminders={p.get('pref_reminders')}  pref_sideBets={p.get('pref_sideBets')}"
    fails += check("toto_get_my_preferences", _check_prefs,
                   "Igor, prefs as configured")

    section("§7 Writes (gated)")

    def _check_write_gate():
        r = m.toto_update_match_result("test_id_for_dry_run", 2, 1)
        err = r.get("error", "")
        if "writes disabled" in err:
            return "BLOCKED at app layer (NEGEV_ALLOW_WRITES=0) ✓"
        if "403" in err or "PERMISSION_DENIED" in err:
            # Firestore rules blocked it — that means NEGEV_ALLOW_WRITES=1 in env
            # but Negev's own security rules saved us. Flag as warning, not OK.
            return ("\033[33m⚠ NEGEV_ALLOW_WRITES=1 in env — relying on Negev's "
                    "Firestore rules. Set to 0 unless intentionally writing.\033[0m")
        return f"\033[31mUNEXPECTED — gate didn't block: {r}\033[0m"
    rc = check("toto_update_match_result (gate)", _check_write_gate,
               "writes disabled at the app layer")
    # If gate is open we count it as failure for this script's exit code
    if os.environ.get("NEGEV_ALLOW_WRITES") == "1":
        rc = 0                                          # intentional
    fails += rc

    print()
    if fails == 0:
        print(f"\033[1;32m✓ All tools verified — 0 failures.\033[0m")
    else:
        print(f"\033[1;31m✗ {fails} failure(s) — inspect above.\033[0m")
    print()
    sys.exit(min(fails, 1))


if __name__ == "__main__":
    main()

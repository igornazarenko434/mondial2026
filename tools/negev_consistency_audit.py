"""Negev Toto vs. our system — full cross-source correlation audit.

Verifies every value our system depends on against what the Negev app uses
server-side. Surface discrepancies WITHOUT auto-fixing — the user must
decide whether to update `config/rules.py` or trust the PDF.

Sections:
  §1  Tournament identity + prize pool ladder
  §2  Scoring multiplier grids (groupStage / round16AndQuarter / semiAndFinal)
  §3  MY_PARTICIPANT vs Negev roster (am I in the leaderboard?)
  §4  Detonator flag — Negev's per-match isDetonator vs our CSV
        (skip when Negev's matches collection has no WC2026 fixtures yet)
  §5  Team name correspondence — Negev catalog vs our canonical
        (skip when Negev's matches collection has no WC2026 fixtures yet)
  §6  Kickoff time correspondence (skip until Negev loads WC2026)

Read-only. ~5 Negev reads. Run on the VM or locally:
    sudo -u mondial bash -c '
        cd /home/mondial/mondial2026
        set -a && source .env && set +a
        PYTHONPATH=. .venv/bin/python tools/negev_consistency_audit.py
    '
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.rules import (
    SCORE_TABLE, TABLE_CAP, PRIZE_LADDER, DETONATOR_FACTOR,
    GROUP_RESET_FACTOR,
)
from integrations import negev_toto_mcp as ntm


GRID_TO_STYPE = {
    "groupStage": "group",
    "round16AndQuarter": "ko",
    "semiAndFinal": "final",
}


def hdr(s):
    bar = "═" * 72
    print(f"\n\033[1;36m{bar}\033[0m\n\033[1;36m  {s}\033[0m\n\033[1;36m{bar}\033[0m")
def ok(s):    print(f"  \033[32m✓\033[0m {s}")
def warn(s):  print(f"  \033[33m⚠\033[0m {s}")
def err(s):   print(f"  \033[31m✗\033[0m {s}")
def info(s):  print(f"    {s}")


# ─────────────────────── §1 Tournament identity ───────────────────────

def audit_tournament(tid: str) -> dict:
    hdr("§1. Tournament identity + prize ladder")
    t = ntm.toto_get_document(f"tournaments/{tid}")
    if "error" in t:
        err(f"can't read tournament {tid}: {t['error']}")
        return {}
    settings = t.get("settings") or {}
    name = t.get("name", "?")
    pool = settings.get("totalPrizePool")
    pct = settings.get("prizePercentages") or []
    ok(f"tournament name: {name!r}, prize pool: ₪{pool}")

    # Cross-check prize percentages vs config/rules.PRIZE_LADDER
    print(f"\n  {'rank':>4}{'Negev %':>10}{'Our %':>10}{'match?':>10}")
    print("  " + "─" * 36)
    diffs = 0
    for rank, ours_frac in PRIZE_LADDER.items():
        negev_pct = pct[rank - 1] if rank - 1 < len(pct) else None
        ours_pct = ours_frac * 100
        match = abs((negev_pct or 0) - ours_pct) < 0.01 if negev_pct is not None else False
        marker = "✓" if match else "✗"
        col = "\033[32m" if match else "\033[31m"
        print(f"  {col}{marker}\033[0m {rank:>2}  {negev_pct!s:>10} {ours_pct:>10.3f}")
        if not match:
            diffs += 1
    if diffs == 0:
        ok("PRIZE_LADDER matches Negev exactly")
    else:
        err(f"{diffs} prize-ladder ranks differ — verify config/rules.py against PDF")

    bonuses = settings.get("kodBonuses")
    info(f"kodBonuses (top-5 ranking bonus): {bonuses}")
    return t


# ─────────────────────── §2 Scoring multiplier grids ───────────────────────

def _negev_cell_to_winner_loser(key: str) -> tuple[int, int] | None:
    """Convert Negev's 'a-b' scoreline key → (winner, loser) tuple. The Negev
    grids encode home-away pairs, but the multipliers are symmetric across
    home/away (same value for 'a-b' and 'b-a'), so we collapse to (max, min).
    '6+-3' → (6, 3); '0-6+' → (6, 0). Returns None on unparseable."""
    try:
        h_raw, a_raw = key.split("-", 1)
        h = int(h_raw.replace("6+", "6"))
        a = int(a_raw.replace("6+", "6"))
        return (max(h, a), min(h, a))
    except (ValueError, AttributeError):
        return None


def _our_value(stype: str, winner: int, loser: int) -> float:
    """Mirror score_match's lookup: explicit cell or TABLE_CAP fallback."""
    return SCORE_TABLE[stype].get((winner, loser), TABLE_CAP[stype])


def audit_scoring_grids(tid: str) -> None:
    hdr("§2. Scoring multiplier grids — Negev managerTables vs config/rules.py")
    g = ntm.toto_get_scoring_grids(tid)
    grids = g.get("grids", {})
    if not grids:
        err("no grids returned")
        return

    for negev_name, stype in GRID_TO_STYPE.items():
        cells = grids.get(negev_name, {})
        if not cells:
            warn(f"grid '{negev_name}' missing from Negev")
            continue
        diffs: list[tuple[str, float, float]] = []
        for key, negev_val in cells.items():
            wl = _negev_cell_to_winner_loser(key)
            if wl is None:
                continue
            ours = _our_value(stype, *wl)
            if abs(float(negev_val) - float(ours)) > 1e-9:
                diffs.append((key, float(negev_val), float(ours)))
        # Collapse symmetric duplicates (Negev "1-0" and "0-1" both → (1,0))
        seen: dict[tuple[int, int], list[tuple[str, float, float]]] = {}
        for key, n, o in diffs:
            wl = _negev_cell_to_winner_loser(key) or (-1, -1)
            seen.setdefault(wl, []).append((key, n, o))
        unique_diff_keys = sorted(seen.keys())
        print(f"\n  \033[1m{negev_name}\033[0m → our '{stype}' table:")
        print(f"    Negev cells: {len(cells)}    unique-pair differences: {len(unique_diff_keys)}")
        if not unique_diff_keys:
            ok(f"all {len(cells)} cells match")
        else:
            err(f"{len(unique_diff_keys)} unique scoreline(s) differ — please verify:")
            print(f"    {'scoreline':<12}{'Negev':>8}{'ours':>8}")
            for wl in unique_diff_keys:
                key, n, o = seen[wl][0]
                # Show as winner-loser form for clarity
                w, l = wl
                tag = f"{w}-{l}"
                print(f"    {tag:<12}{n:>8}{o:>8}")
        # Sanity check: a few known-good cells
        for key, expected_msg in [("1-1", "draw 1-1"), ("0-0", "draw 0-0")]:
            wl = _negev_cell_to_winner_loser(key)
            if wl and key in cells:
                n = float(cells[key]); o = _our_value(stype, *wl)
                if abs(n - o) < 1e-9:
                    info(f"  {expected_msg}: Negev={n} ours={o} ✓")


# ─────────────────────── §3 MY_PARTICIPANT vs roster ───────────────────────

def audit_bots(tid: str) -> None:
    """Surface the known bots and verify our standings excludes them."""
    hdr("§2.5  Bot accounts — must be excluded from standings")
    # Read all users + count bots vs humans for this tournament
    users = ntm._read_all("users")
    in_tournament = [u for u in users if tid in (u.get("tournaments") or [])]
    bots = [u for u in in_tournament if ntm._is_bot(u)]
    humans = [u for u in in_tournament if not ntm._is_bot(u)]
    ok(f"{len(in_tournament)} total in tournament  =  "
       f"{len(humans)} humans  +  {len(bots)} bots")
    if bots:
        print("\n  Bots (excluded from our standings, role+isBot+uid-prefix all match):")
        for b in sorted(bots, key=lambda x: -float(x.get("pointsTotal") or 0)):
            print(f"    {b.get('displayName','?'):<22} uid={b.get('uid','?'):<20}"
                  f" role={b.get('role','?'):<8} isBot={b.get('isBot')!s:<6}"
                  f" pointsTotal={b.get('pointsTotal')}")
    # Sanity: run toto_get_standings and confirm zero bots in the result
    rows = ntm.toto_get_standings(tid)
    leaked = [r for r in rows if r["uid"] and r["uid"].startswith("bot_")]
    if leaked:
        err(f"{len(leaked)} bot row(s) leaked into toto_get_standings — fix _is_bot")
    else:
        ok("toto_get_standings correctly returns 0 bot rows by default")


def audit_me(tid: str) -> None:
    hdr("§3. MY_PARTICIPANT vs Negev roster")
    me = os.environ.get("MY_PARTICIPANT", "").strip()
    if not me:
        err("MY_PARTICIPANT not set in .env — strategy layer won't activate")
        return
    rows = ntm.toto_get_standings(tid)
    by_name = {r["player"]: r for r in rows}
    if me in by_name:
        r = by_name[me]
        ok(f"{me!r} found in Negev roster: rank {r['rank']}/{len(rows)}, "
           f"{r['total']:.0f} pts (direction {r['direction']:.0f} + broad {r['broad']:.0f})")
    else:
        err(f"MY_PARTICIPANT={me!r} NOT in Negev roster of {len(rows)} players")
        # Suggest near-matches
        candidates = [n for n in by_name if me.lower() in n.lower() or n.lower() in me.lower()]
        if candidates:
            info(f"  similar names in roster: {candidates[:5]}")


# ─────────────────────── §4-6 Match catalog ───────────────────────

def audit_match_catalog(tid: str) -> None:
    hdr("§4-6. Negev match catalog — WC 2026 fixtures, detonators, names, times")
    # Negev's matches collection is GLOBAL (J-League, Allsvenskan, etc. all
    # mixed in). Filter to WC2026 stage labels only — Group/R32/R16/QF/SF/
    # 3rd/Final per our _STAGE_MAP. If Negev hasn't tagged any matches with
    # those stages yet, the WC fixtures aren't loaded yet.
    matches = ntm.toto_get_matches(date_after="2026-06-01", limit=200)
    wc_stages = {"Group", "R32", "R16", "QF", "SF", "3rd", "Final"}
    wc2026 = [m for m in matches if m.get("stage") in wc_stages
              and (m.get("kickoff_utc") or "") >= "2026-06-01"]
    if not wc2026:
        info(f"Negev's matches collection has {len(matches)} >= 2026-06-01 entries")
        info("but NONE are tagged with WC-2026 stages (Group/R32/R16/QF/SF/Final).")
        info("The founder hasn't loaded the WC fixtures yet. Re-run this audit")
        info("~24h before the opener (around 2026-06-10 22:00 IDT) to verify")
        info("team names + kickoff times + detonator flags.")
        return
    print(f"  found {len(wc2026)} WC 2026 fixtures in Negev. Comparing to our DB…")
    # When live, compare each Negev fixture to our matches table:
    # - team names (after teams.normalize on both sides)
    # - kickoff_utc (exact ISO match)
    # - isDetonator
    # - stage label after _STAGE_MAP
    import sqlite3
    from store.db import connect
    conn = connect()
    diffs = []
    for m in wc2026[:20]:
        ours = conn.execute(
            "SELECT match_id, home, away, utc_kickoff, stage, detonator "
            "FROM matches WHERE home=? AND away=?",
            (m["home"], m["away"])).fetchone()
        if not ours:
            diffs.append(("not in our matches", m))
            continue
        if (ours["utc_kickoff"] or "")[:19] != (m["kickoff_utc"] or "")[:19]:
            diffs.append(("kickoff mismatch", m))
        if bool(ours["detonator"]) != bool(m.get("isDetonator")):
            diffs.append(("detonator flag mismatch", m))
        if ours["stage"] != m.get("stage"):
            diffs.append(("stage mismatch", m))
    if not diffs:
        ok(f"first 20 Negev fixtures all match our DB (team names + kickoff + detonator + stage)")
    else:
        err(f"{len(diffs)} discrepancies in first 20 fixtures:")
        for reason, m in diffs[:10]:
            info(f"  {reason}: {m.get('home')} vs {m.get('away')} ({m.get('kickoff_utc')})")


# ─────────────────────── main ───────────────────────

def main():
    tid = os.environ.get("NEGEV_TOURNAMENT_ID", "").strip()
    if not tid:
        print("error: NEGEV_TOURNAMENT_ID not set in .env", file=sys.stderr)
        sys.exit(2)
    print(f"\n\033[1mNegev Toto Consistency Audit — tournament {tid}\033[0m")

    audit_tournament(tid)
    audit_scoring_grids(tid)
    audit_bots(tid)
    audit_me(tid)
    audit_match_catalog(tid)

    print("\n\033[1;32m✓ Audit complete.\033[0m\n")


if __name__ == "__main__":
    main()

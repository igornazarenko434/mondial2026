"""Data-provenance audit — trace every external source through storage
to the formula that consumes it.

For each source we verify:
  * shape check     — expected columns present in the store with expected types
  * row-count check — at least one populated row (or a documented reason
                       for zero — e.g. pre-tournament, no matches yet)
  * cross-source ID consistency — the same match resolves to the same key
                                    across football-data / api-football /
                                    Negev, joinable without ambiguity
  * formula spot-check — pluck a real row and run the formula that uses
                          it, printing intermediate + final values so a
                          human can eyeball correctness

Read-only. Safe to run any time. Costs ~2 Negev calls + zero football-data /
odds-api / api-football / Brave / LLM calls (everything else reads local
stores).

  PYTHONPATH=. .venv/bin/python tools/audit_data_flow.py
"""
from __future__ import annotations
import csv
import json
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = "store/mondial.db"


def hdr(s: str) -> None:
    print()
    print("=" * 72)
    print(f"  {s}")
    print("=" * 72)


def _cols(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """Return {column_name: declared_type} via PRAGMA table_info."""
    try:
        return {r[1]: r[2] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return {}


def _rows(conn: sqlite3.Connection, sql: str, *params) -> list[sqlite3.Row]:
    try:
        cur = conn.execute(sql, params)
        cur.row_factory = sqlite3.Row
        return cur.fetchall()
    except sqlite3.Error as e:
        print(f"    (SQL error: {e})")
        return []


# ────────────────────────────────────────────────────────────────────────
# 1. SCHEMA — every expected column present in the right table with correct type
# ────────────────────────────────────────────────────────────────────────
def audit_schema(conn: sqlite3.Connection) -> dict:
    hdr("1. SCHEMA — declared columns vs code expectations")

    # (table, {column: expected_type_or_None}) — None = don't care about type
    expected = {
        "matches": {
            "match_id": "INTEGER", "utc_kickoff": "TEXT",
            "local_kickoff": "TEXT", "stage": "TEXT", "grp": "TEXT",
            "home": "TEXT", "away": "TEXT", "status": "TEXT",
            "home_goals": "INTEGER", "away_goals": "INTEGER",
            "detonator": "INTEGER",
            "penalty_home": "INTEGER", "penalty_away": "INTEGER",
        },
        "odds_snapshots": {
            "match_id": "INTEGER", "captured_at": "TEXT", "book": "TEXT",
            "odds_h": "REAL", "odds_d": "REAL", "odds_a": "REAL",
        },
        "predictions": {
            "match_id": "INTEGER", "created_at": "TEXT", "window": "TEXT",
            "pick_dir": "TEXT", "pick_h": "INTEGER", "pick_a": "INTEGER",
            "modal_h": "INTEGER", "modal_a": "INTEGER",
            "expected_points": "REAL", "payload_json": "TEXT",
        },
        "standings": {
            "participant": "TEXT", "group_points": "REAL",
            "knockout_points": "REAL", "futures_points": "REAL",
            "side_points": "REAL", "role": "TEXT", "negev_rank": "INTEGER",
        },
        "runs": {
            "match_id": "INTEGER", "window": "TEXT", "status": "TEXT",
            "correlation_id": "TEXT",
        },
        "api_calls": {
            "provider": "TEXT", "endpoint": "TEXT", "units": "REAL",
            "correlation_id": "TEXT", "duration_ms": "REAL",
        },
    }

    ok = True
    for tbl, cols_exp in expected.items():
        cols_actual = _cols(conn, tbl)
        rows = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        missing = [c for c in cols_exp if c not in cols_actual]
        wrong_type = []
        for c, t_exp in cols_exp.items():
            t_act = cols_actual.get(c)
            if t_act and t_exp and t_act.upper() != t_exp.upper():
                wrong_type.append(f"{c} (expected {t_exp}, got {t_act})")

        status = "OK" if not missing and not wrong_type else "DRIFT"
        print(f"  {tbl:<20} rows={rows:>6}  status={status}")
        if missing:
            print(f"    ! missing: {missing}")
            ok = False
        if wrong_type:
            print(f"    ! wrong type: {wrong_type}")
            ok = False

    return {"schema_ok": ok}


# ────────────────────────────────────────────────────────────────────────
# 2. FOOTBALL-DATA.ORG → matches table
# ────────────────────────────────────────────────────────────────────────
def audit_football_data(conn: sqlite3.Connection) -> dict:
    hdr("2. FOOTBALL-DATA.ORG → matches table")

    total = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    by_stage = conn.execute(
        "SELECT stage, COUNT(*) FROM matches GROUP BY stage ORDER BY stage"
    ).fetchall()
    by_status = conn.execute(
        "SELECT status, COUNT(*) FROM matches GROUP BY status"
    ).fetchall()

    print(f"  Total rows: {total}")
    print("  By stage:  ", ", ".join(f"{s}={n}" for s, n in by_stage))
    print("  By status: ", ", ".join(f"{s}={n}" for s, n in by_status))

    # Spot check: pens fields wired correctly?
    pen_rows = conn.execute(
        "SELECT home, away, home_goals, away_goals, penalty_home, penalty_away "
        "FROM matches WHERE penalty_home IS NOT NULL "
        "ORDER BY utc_kickoff DESC LIMIT 5"
    ).fetchall()
    print(f"  Matches with penalty tally: {len(pen_rows)}")
    for r in pen_rows:
        print(f"    {r[0]} vs {r[1]}: 120'={r[2]}-{r[3]}  pens={r[4]}-{r[5]}")

    return {"matches_total": total,
            "matches_pens": len(pen_rows),
            "stages": {s: n for s, n in by_stage}}


# ────────────────────────────────────────────────────────────────────────
# 3. THE-ODDS-API → odds_snapshots + devig formula
# ────────────────────────────────────────────────────────────────────────
def audit_odds(conn: sqlite3.Connection) -> dict:
    hdr("3. THE-ODDS-API → odds_snapshots (devig on read)")

    total = conn.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
    by_book = conn.execute(
        "SELECT book, COUNT(*) FROM odds_snapshots GROUP BY book "
        "ORDER BY COUNT(*) DESC"
    ).fetchall()
    by_window = conn.execute(
        "SELECT captured_at, COUNT(*) FROM odds_snapshots "
        "GROUP BY captured_at ORDER BY COUNT(*) DESC LIMIT 6"
    ).fetchall()

    print(f"  Total snapshots: {total}")
    print("  By book:   ", ", ".join(f"{b or '?'}={n}" for b, n in by_book))
    print("  By window: ", ", ".join(f"{w or '?'}={n}" for w, n in by_window))

    # Devig sanity — pull one latest snapshot and check probabilities sum ~1.
    from core.data.oddsapi import devig
    r = conn.execute(
        "SELECT m.home, m.away, o.book, o.captured_at, o.odds_h, o.odds_d, o.odds_a "
        "FROM odds_snapshots o JOIN matches m ON o.match_id = m.match_id "
        "WHERE o.odds_h IS NOT NULL "
        "ORDER BY o.captured_at DESC LIMIT 1"
    ).fetchone()
    if r:
        h, a, book, cap, oh, od, oa = r
        p = devig({"H": oh, "D": od, "A": oa})
        s = p["H"] + p["D"] + p["A"]
        print(f"  Devig spot check on {h} vs {a}  ({book}, {cap}):")
        print(f"    odds  H={oh:.2f}  D={od:.2f}  A={oa:.2f}")
        print(f"    devig H={p['H']:.3f} D={p['D']:.3f} A={p['A']:.3f}  Σ={s:.4f}")
        assert abs(s - 1.0) < 0.01, f"devig invariant broken: {s}"
    return {"odds_total": total}


# ────────────────────────────────────────────────────────────────────────
# 4. PREDICTIONS TABLE — audit-trail JSON completeness
# ────────────────────────────────────────────────────────────────────────
def audit_predictions(conn: sqlite3.Connection) -> dict:
    hdr("4. PREDICTIONS → payload_json audit trail")

    total = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    by_window = conn.execute(
        "SELECT window, COUNT(*) FROM predictions GROUP BY window "
        "ORDER BY COUNT(*) DESC"
    ).fetchall()

    print(f"  Total predictions: {total}")
    print("  By window: ", ", ".join(f"{w or '?'}={n}" for w, n in by_window))

    required = {"signals_used", "signals_failed",
                "pick_direction", "pick_exact_score", "expected_points",
                "correlation_id"}

    rows = conn.execute(
        "SELECT match_id, window, created_at, payload_json FROM predictions "
        "WHERE window='T-7m' ORDER BY created_at DESC LIMIT 3"
    ).fetchall()

    for mid, w, cap, payload in rows:
        try:
            p = json.loads(payload) if payload else {}
        except Exception as e:                              # noqa: BLE001
            print(f"  ! {mid}: JSON parse: {e}")
            continue
        missing = sorted(required - set(p.keys()))
        pick = p.get("pick_exact_score") or {}
        print(f"  match_id={mid:<6} {w:<6} pick={pick.get('home')}-{pick.get('away')} "
              f"dir={p.get('pick_direction')} EV={p.get('expected_points') or 0:.2f}  "
              f"missing={missing or 'NONE'}")
    return {"pred_total": total}


# ────────────────────────────────────────────────────────────────────────
# 5. STANDINGS — Negev mirror parity
# ────────────────────────────────────────────────────────────────────────
def audit_standings(conn: sqlite3.Connection) -> dict:
    hdr("5. STANDINGS → local mirror vs Negev live")

    from integrations import negev_toto_mcp as ntm
    from core import obs
    try:
        with obs.external_call("negev_toto", "get_standings"):
            neg = ntm.toto_get_standings(include_bots=True)
    except Exception as e:                                  # noqa: BLE001
        print(f"  ! Negev fetch failed: {e}")
        return {"standings_ok": False}

    neg_map = {r.get("player"): r for r in neg}
    total_local = conn.execute("SELECT COUNT(*) FROM standings").fetchone()[0]
    print(f"  Negev: {len(neg)} players (incl. bots)")
    print(f"  Local: {total_local} rows")

    tracked = ("Igor", "Vaadia")
    for name in tracked:
        row = conn.execute(
            "SELECT group_points, knockout_points, futures_points, "
            "       COALESCE(side_points,0) AS s, negev_rank "
            "FROM standings WHERE participant=?", (name,)
        ).fetchone()
        if not row:
            print(f"  ! {name}: not in local standings")
            continue
        local_total = sum(row[:4])
        n = neg_map.get(name, {})
        n_total = float(n.get("total") or 0)
        drift = abs(local_total - n_total)
        flag = "OK" if drift < 0.05 else "DRIFT"
        print(f"  {name:<8} local={local_total:>7.2f}  negev={n_total:>7.2f}  "
              f"rank(local)={row[4]}  rank(negev)={n.get('rank')}  {flag}")
    return {"standings_ok": True}


# ────────────────────────────────────────────────────────────────────────
# 6. ELO CACHE — every WC 2026 team has a rating
# ────────────────────────────────────────────────────────────────────────
def audit_elo() -> dict:
    hdr("6. ELO CACHE (eloratings.net) → soccerdata_io / blend weight 0.20")
    from core.data.soccerdata_io import national_team_elo
    from core.data.teams import normalize
    elo = national_team_elo()
    print(f"  Elo cache: {len(elo)} teams")
    roster = set()
    csv_path = "data/wc2026_groups.csv"
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            roster.add(normalize(r["team"]))
    missing = sorted(t for t in roster if t not in elo)
    print(f"  WC roster: {len(roster)} teams  ·  missing from Elo cache: "
          f"{missing or 'none'}")
    # Sample lookups
    for t in ["Brazil", "France", "Uzbekistan", "Curacao"]:
        print(f"    {t:<12} elo={elo.get(normalize(t))}")
    return {"elo_missing": len(missing)}


# ────────────────────────────────────────────────────────────────────────
# 7. DIXON-COLES → attack/defence strengths → expected goals
# ────────────────────────────────────────────────────────────────────────
def audit_dc() -> dict:
    hdr("7. DIXON-COLES (martj42 CSV → strengths → λ) / blend weight 0.20")
    from core.data.results_io import historical_results
    from core.models.fit import cached_strengths, expected_goals_fn
    results = historical_results()
    s = cached_strengths(results)
    print(f"  Historical results: {len(results)} rows")
    print(f"  DC strengths cache: {len(s)} teams")
    eg = expected_goals_fn(s)
    for h, a in [("Brazil", "Japan"), ("France", "Spain"),
                 ("Argentina", "Egypt")]:
        try:
            lh, la = eg(h, a)
            print(f"    λ({h:<12} vs {a:<12}) = ({lh:.3f}, {la:.3f})")
        except Exception as e:                              # noqa: BLE001
            print(f"    ! λ({h}, {a}): {e}")
    return {"dc_ok": True}


# ────────────────────────────────────────────────────────────────────────
# 8. CROSS-SOURCE IDs — same match, three IDs, joinable?
# ────────────────────────────────────────────────────────────────────────
def audit_cross_source_ids(conn: sqlite3.Connection) -> dict:
    hdr("8. CROSS-SOURCE IDs — same match across football-data / API-Football / Negev")
    from integrations import negev_toto_mcp as ntm
    from core import obs
    try:
        with obs.external_call("negev_toto", "get_matches"):
            negev = ntm.toto_get_matches(limit=200)
    except Exception as e:                                  # noqa: BLE001
        print(f"  ! Negev fetch failed: {e}")
        return {}

    for pair in [("Brazil", "Japan"), ("Argentina", "Egypt"),
                 ("Portugal", "Spain")]:
        row = conn.execute(
            "SELECT match_id FROM matches WHERE home=? AND away=?", pair
        ).fetchone()
        neg = next((m for m in negev
                    if m.get("home") == pair[0] and m.get("away") == pair[1]),
                    None)
        fd_id = row[0] if row else "(missing)"
        af_id = neg.get("apiFixtureId") if neg else "(missing)"
        neg_id = neg.get("match_id") if neg else "(missing)"
        print(f"  {pair[0]:<12} vs {pair[1]:<12}  "
              f"fd_id={fd_id}  af_id={af_id}  negev_doc={neg_id}")
    return {}


# ────────────────────────────────────────────────────────────────────────
# 9. SCORING FORMULA — run score_match on a real finished pens row
# ────────────────────────────────────────────────────────────────────────
def audit_scoring_formula(conn: sqlite3.Connection) -> dict:
    hdr("9. SCORING FORMULA — score_match on a real finished PEN row")
    from core.scoring.engine import score_match, direction, exact_multiplier
    r = conn.execute(
        "SELECT m.home, m.away, m.stage, m.home_goals, m.away_goals, "
        "       m.penalty_home, m.penalty_away, "
        "       o.odds_h, o.odds_d, o.odds_a "
        "FROM matches m LEFT JOIN odds_snapshots o "
        "  ON m.match_id = o.match_id AND o.captured_at LIKE 'T-7m%' "
        "WHERE m.status='FINISHED' AND m.penalty_home IS NOT NULL "
        "  AND o.odds_h IS NOT NULL "
        "ORDER BY m.utc_kickoff DESC LIMIT 1"
    ).fetchone()
    if not r:
        print("  (no finished PEN match with T-7m odds — nothing to check)")
        return {}
    h, a, st, hg, ag, ph, pa, oh, od, oa = r
    odds = {"H": oh, "D": od, "A": oa}
    print(f"  Match: {h} vs {a}  stage={st}")
    print(f"    Stored 120' score: {hg}-{ag}   pens: {ph}-{pa}")
    print(f"    odds(H,D,A) = ({oh:.2f}, {od:.2f}, {oa:.2f})")
    print(f"    direction({hg},{ag}) = {direction(hg, ag)}")
    print(f"    exact_multiplier(ko, {hg}, {ag}) = "
          f"{exact_multiplier('ko', hg, ag)}")
    for pick_h, pick_a in [(hg, ag), (2, 1), (1, 0)]:
        pts = score_match(st, pick_h, pick_a, hg, ag, odds)
        print(f"    score_match(pred={pick_h}-{pick_a}, actual={hg}-{ag}) = "
              f"{pts:.3f} pts")
    return {"formula_ok": True}


# ────────────────────────────────────────────────────────────────────────
# 10. COST LEDGER — every provider recorded, all calls tagged
# ────────────────────────────────────────────────────────────────────────
def audit_cost_ledger(conn: sqlite3.Connection) -> dict:
    hdr("10. COST LEDGER — every external call tagged with a provider")
    rows = conn.execute(
        "SELECT provider, COUNT(*) AS n, SUM(units) AS units "
        "FROM api_calls GROUP BY provider ORDER BY n DESC"
    ).fetchall()
    if not rows:
        print("  (no api_calls rows — ledger empty)")
        return {}
    for r in rows:
        prov = r[0] or "(untagged)"
        print(f"    {prov:<20} calls={r[1]:>6}  units={r[2] or 0:>8.1f}")
    return {}


def main() -> int:
    from store.db import connect
    conn = connect()
    audits = [
        ("schema", lambda: audit_schema(conn)),
        ("football_data", lambda: audit_football_data(conn)),
        ("odds", lambda: audit_odds(conn)),
        ("predictions", lambda: audit_predictions(conn)),
        ("standings", lambda: audit_standings(conn)),
        ("elo", lambda: audit_elo()),
        ("dc", lambda: audit_dc()),
        ("cross_ids", lambda: audit_cross_source_ids(conn)),
        ("formula", lambda: audit_scoring_formula(conn)),
        ("cost_ledger", lambda: audit_cost_ledger(conn)),
    ]
    for name, fn in audits:
        try:
            fn()
        except Exception as e:                              # noqa: BLE001
            print(f"\n  !! {name} raised: {type(e).__name__}: {e}")
    conn.close()
    print()
    print("=" * 72)
    print("  Audit complete.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())

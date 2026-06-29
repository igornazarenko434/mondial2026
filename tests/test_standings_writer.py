"""Day-5: results → score_match → standings.

Tests the full wiring chain offline: in-memory SQLite using the project's real
schema, seeded matches + predictions + odds_snapshots, then update_standings()
must produce the right totals (matching the PDF worked examples), apply the
-15% group reset only when KO games are scored, and stay idempotent on re-run.
"""
from __future__ import annotations
import sqlite3
import pytest
from core.scoring.standings_writer import (
    update_standings, compute_prize_distribution, score_one_match
)
from core.data.oddsapi import snapshot_odds


def _schema_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        conn.executescript(f.read())
    return conn


def _seed_match(conn, match_id, home, away, stage, hg, ha,
                detonator=0, status="FINISHED"):
    conn.execute(
        "INSERT INTO matches (match_id, utc_kickoff, stage, grp, home, away, "
        "status, home_goals, away_goals, detonator) "
        "VALUES (?, '2026-06-11T19:00:00Z', ?, 'A', ?, ?, ?, ?, ?, ?)",
        (match_id, stage, home, away, status, hg, ha, detonator))


def _seed_prediction(conn, match_id, pick_h, pick_a, direction,
                     window="T-7m"):
    conn.execute(
        "INSERT INTO predictions (match_id, created_at, window, pick_dir, "
        "pick_h, pick_a, modal_h, modal_a, expected_points, payload_json) "
        "VALUES (?, '2026-06-11T18:53Z', ?, ?, ?, ?, ?, ?, 1.0, '{}')",
        (match_id, window, direction, pick_h, pick_a, pick_h, pick_a))


# ---------- score_one_match (the per-match unit) ----------

def test_score_one_match_pdf_example_france_2_1():
    """PDF worked example: group stage, exact France 2-1 win, odds 2.0 →
    multiplier 1.5 × 2.0 = 3.000 (no detonator)."""
    conn = _schema_conn()
    _seed_match(conn, 1, "France", "Spain", "Group", 2, 1, detonator=0)
    _seed_prediction(conn, 1, 2, 1, "H")
    snapshot_odds(conn, 1, "T-7m", "pinnacle", {"H": 2.0, "D": 2.5, "A": 1.5})
    m = conn.execute("SELECT * FROM matches WHERE match_id=1").fetchone()
    pts = score_one_match(conn, m, participant="me")
    assert pts == 3.0       # 1.5 (exact) × 2.0 (odds) — PDF §12 worked example


def test_score_one_match_returns_none_without_odds():
    conn = _schema_conn()
    _seed_match(conn, 1, "Mexico", "South Africa", "Group", 1, 0)
    _seed_prediction(conn, 1, 1, 0, "H")
    # no snapshot_odds → can't score
    m = conn.execute("SELECT * FROM matches WHERE match_id=1").fetchone()
    assert score_one_match(conn, m, participant="me") is None


def test_score_one_match_returns_none_without_prediction():
    conn = _schema_conn()
    _seed_match(conn, 1, "Mexico", "South Africa", "Group", 1, 0)
    snapshot_odds(conn, 1, "T-7m", "pinnacle", {"H": 1.5, "D": 3.0, "A": 5.0})
    m = conn.execute("SELECT * FROM matches WHERE match_id=1").fetchone()
    assert score_one_match(conn, m, participant="me") is None


# ---------- update_standings: golden path + reset + persistence ----------

def test_update_standings_aggregates_group_points():
    """Two group games, both exact-score wins → both scored, totals correct."""
    conn = _schema_conn()
    # Game 1: France 2-1, odds 2.0 → 3.0 points
    _seed_match(conn, 1, "France", "Spain", "Group", 2, 1)
    _seed_prediction(conn, 1, 2, 1, "H")
    snapshot_odds(conn, 1, "T-7m", "pinnacle", {"H": 2.0, "D": 2.5, "A": 1.5})
    # Game 2: Spain 1-1 draw exact, draw odds 2.5 → 2.25 × 2.5 = 5.625 (PDF)
    _seed_match(conn, 2, "Spain", "Germany", "Group", 1, 1)
    _seed_prediction(conn, 2, 1, 1, "D")
    snapshot_odds(conn, 2, "T-7m", "pinnacle", {"H": 2.0, "D": 2.5, "A": 3.0})

    res = update_standings(conn)
    assert res["scored_matches"] == 2
    assert res["knockout_points"] == 0.0
    assert res["group_points"] == round(3.0 + 5.625, 3)
    # No KO games scored → no -15% reset applied
    assert res["total"] == res["group_points"]


def test_update_standings_applies_minus_15_after_groups():
    """When any KO game is scored, -15% reset hits the group total."""
    conn = _schema_conn()
    # Group game scoring 3.0
    _seed_match(conn, 1, "France", "Spain", "Group", 2, 1)
    _seed_prediction(conn, 1, 2, 1, "H")
    snapshot_odds(conn, 1, "T-7m", "pinnacle", {"H": 2.0, "D": 2.5, "A": 1.5})
    # KO game scoring 1.5 (base ko=1.5 × wrong-exact 1.0... actually direction-only with odds 2.0)
    # Let's do an exact knockout score: R16 1-0 home win, table value 2.25, odds 2.0 → 4.5
    _seed_match(conn, 2, "France", "Brazil", "R16", 1, 0)
    _seed_prediction(conn, 2, 1, 0, "H")
    snapshot_odds(conn, 2, "T-7m", "pinnacle", {"H": 2.0, "D": 2.5, "A": 3.5})

    res = update_standings(conn)
    assert res["scored_matches"] == 2
    # group: 3.0 × 0.85 = 2.55  (apply_group_reset)
    assert res["group_points"] == 2.55
    # KO points untouched by reset
    assert res["knockout_points"] > 0
    assert res["total"] == round(res["group_points"] + res["knockout_points"], 3)


def test_update_standings_idempotent():
    """Running twice gives the same DB STATE — no double-counting.

    Day-9.27 contract change: the SECOND call now returns
    `written_to_db=False` (Negev-row guard kicks in), but the database
    state is identical. Idempotency is asserted on the DB, not on the
    return dict."""
    conn = _schema_conn()
    _seed_match(conn, 1, "France", "Spain", "Group", 2, 1)
    _seed_prediction(conn, 1, 2, 1, "H")
    snapshot_odds(conn, 1, "T-7m", "pinnacle", {"H": 2.0, "D": 2.5, "A": 1.5})

    a = update_standings(conn)
    b = update_standings(conn)
    # Computed totals are identical
    assert a["group_points"] == b["group_points"]
    assert a["knockout_points"] == b["knockout_points"]
    assert a["total"] == b["total"]
    # Second call doesn't write (Negev-row guard fires)
    assert a["written_to_db"] is True
    assert b["written_to_db"] is False
    rows = conn.execute("SELECT COUNT(*) FROM standings WHERE participant='me'"
                        ).fetchone()
    assert rows[0] == 1   # upsert, not duplicate


def test_update_standings_persists_to_standings_table():
    conn = _schema_conn()
    _seed_match(conn, 1, "France", "Spain", "Group", 2, 1)
    _seed_prediction(conn, 1, 2, 1, "H")
    snapshot_odds(conn, 1, "T-7m", "pinnacle", {"H": 2.0, "D": 2.5, "A": 1.5})
    update_standings(conn, participant="igor")
    row = conn.execute(
        "SELECT participant, group_points, knockout_points, futures_points "
        "FROM standings WHERE participant='igor'").fetchone()
    assert row is not None
    assert row["group_points"] == 3.0
    assert row["knockout_points"] == 0.0
    assert row["futures_points"] == 0.0


def test_update_standings_preserves_futures_points():
    """Day-7 may have written futures_points; update_standings must NOT clobber."""
    conn = _schema_conn()
    conn.execute("INSERT INTO standings (participant, group_points, "
                 "knockout_points, futures_points) VALUES ('me', 0, 0, 17.5)")
    _seed_match(conn, 1, "France", "Spain", "Group", 2, 1)
    _seed_prediction(conn, 1, 2, 1, "H")
    snapshot_odds(conn, 1, "T-7m", "pinnacle", {"H": 2.0, "D": 2.5, "A": 1.5})
    res = update_standings(conn)
    assert res["futures_points"] == 17.5
    # And persisted
    row = conn.execute("SELECT futures_points FROM standings WHERE "
                        "participant='me'").fetchone()
    assert row["futures_points"] == 17.5


def test_skip_unfinished_matches():
    """Only FINISHED matches with both goals should be scored."""
    conn = _schema_conn()
    _seed_match(conn, 1, "France", "Spain", "Group", 2, 1, status="FINISHED")
    _seed_prediction(conn, 1, 2, 1, "H")
    snapshot_odds(conn, 1, "T-7m", "pinnacle", {"H": 2.0, "D": 2.5, "A": 1.5})
    # not-yet-played match
    _seed_match(conn, 2, "Norway", "France", "Group", None, None, status="TIMED")
    _seed_prediction(conn, 2, 1, 2, "A")
    snapshot_odds(conn, 2, "T-7m", "pinnacle", {"H": 4.0, "D": 3.5, "A": 1.8})

    res = update_standings(conn)
    assert res["scored_matches"] == 1     # only the FINISHED one
    assert res["group_points"] == 3.0


# ---------- robustness: never crash on bad data ----------

def test_direction_only_correct_gets_base_times_odds():
    """Predicted 1-0, actual 2-1 (both home wins, different exact). Group
    base = 1.0, so 1.0 × odds 2.0 = 2.0 points (PDF §12a)."""
    conn = _schema_conn()
    _seed_match(conn, 1, "France", "Spain", "Group", 2, 1)
    _seed_prediction(conn, 1, 1, 0, "H")
    snapshot_odds(conn, 1, "T-7m", "pinnacle", {"H": 2.0, "D": 2.5, "A": 3.0})
    res = update_standings(conn)
    assert res["group_points"] == 2.0


def test_wrong_direction_scores_zero():
    """Pick home win when result was draw → 0 points (PDF wrong-direction rule)."""
    conn = _schema_conn()
    _seed_match(conn, 1, "France", "Spain", "Group", 1, 1)
    _seed_prediction(conn, 1, 2, 1, "H")     # predicted home win
    snapshot_odds(conn, 1, "T-7m", "pinnacle", {"H": 2.0, "D": 2.5, "A": 3.0})
    res = update_standings(conn)
    assert res["group_points"] == 0.0


def test_unknown_stage_label_does_not_crash():
    """If football-data introduces a stage code we don't recognize, score_match
    raises ValueError — score_one_match must catch and skip with a warning so
    the daemon stays alive (CLAUDE.md golden rule #8)."""
    conn = _schema_conn()
    _seed_match(conn, 1, "France", "Spain", "TOTALLY_NEW_STAGE_FORMAT", 2, 1)
    _seed_prediction(conn, 1, 2, 1, "H")
    snapshot_odds(conn, 1, "T-7m", "pinnacle", {"H": 2.0, "D": 2.5, "A": 3.0})
    # Must NOT raise; problematic match is silently skipped.
    res = update_standings(conn)
    assert res["scored_matches"] == 0
    assert res["group_points"] == 0.0


def test_pdf_examples_via_update_standings_end_to_end():
    """Pin all three PDF worked examples through the full standings_writer
    path (not just score_match): France 2-1 = 3.000, draw 1-1 = 5.625,
    Final 2-2 = 9.375.

    Day-9.33 (2026-06-29): Final 2-2 expected dropped from 12.5 → 9.375
    after Negev re-priced semiAndFinal cell [2][2] from 5 to 3.75 (×2.5 odds).
    The PDF's original 12.5 example is now stale vs Negev's live grid;
    Negev's server-side scoring grid is our source of truth."""
    for stage, ph, pa, ah, aa, odds, expected_bucket in [
        ("Group", 2, 1, 2, 1, {"H": 2.0, "D": 2.5, "A": 1.5}, ("group_points",  3.0)),
        ("Group", 1, 1, 1, 1, {"H": 2.0, "D": 2.5, "A": 3.0}, ("group_points",  5.625)),
        ("Final", 2, 2, 2, 2, {"H": 2.0, "D": 2.5, "A": 2.0}, ("knockout_points", 9.375)),
    ]:
        conn = _schema_conn()
        _seed_match(conn, 1, "X", "Y", stage, ah, aa)
        _seed_prediction(conn, 1, ph, pa,
                         "H" if ph > pa else "D" if ph == pa else "A")
        snapshot_odds(conn, 1, "T-7m", "pinnacle", odds)
        res = update_standings(conn, apply_reset_after_groups=False)
        bucket, expected = expected_bucket
        assert res[bucket] == expected, \
            f"{stage} {ph}-{pa} actual {ah}-{aa}: expected {expected} got {res[bucket]}"


def test_null_detonator_field_treated_as_false():
    """football-data may not always set the detonator flag; SQLite NULL must
    not be ambiguously coerced (bool(None) is False — verify it actually is)."""
    conn = _schema_conn()
    conn.execute("INSERT INTO matches (match_id, utc_kickoff, stage, grp, home, "
                 "away, status, home_goals, away_goals, detonator) "
                 "VALUES (1, '2026-06-11T19:00Z', 'Group', 'A', 'X', 'Y', "
                 "'FINISHED', 2, 1, NULL)")
    _seed_prediction(conn, 1, 2, 1, "H")
    snapshot_odds(conn, 1, "T-7m", "pinnacle", {"H": 2.0, "D": 2.5, "A": 3.0})
    res = update_standings(conn)
    assert res["group_points"] == 3.0    # not 6.0 (× detonator)


def test_extreme_scoreline_uses_table_cap():
    """An absurd scoreline (8-7 with no entry in the printed table) must
    fall back to the per-stage TABLE_CAP, not crash. Confirms the engine's
    .get((w, l), TABLE_CAP[stype]) fallback is exercised end-to-end."""
    conn = _schema_conn()
    _seed_match(conn, 1, "X", "Y", "Group", 8, 7)
    _seed_prediction(conn, 1, 8, 7, "H")
    snapshot_odds(conn, 1, "T-7m", "pinnacle", {"H": 100.0, "D": 50.0, "A": 1.5})
    res = update_standings(conn)
    # Must score positive (TABLE_CAP * 100) without crashing — actual value
    # is grid-dependent; just assert reasonable range.
    assert res["group_points"] > 100   # non-zero & uses big odds
    assert res["scored_matches"] == 1


def test_odds_snapshot_with_null_field_skipped():
    """A snapshot row with NULL in H/D/A is unusable → score_one_match returns
    None instead of raising on the arithmetic."""
    conn = _schema_conn()
    _seed_match(conn, 1, "X", "Y", "Group", 2, 1)
    _seed_prediction(conn, 1, 2, 1, "H")
    conn.execute("INSERT INTO odds_snapshots (match_id, captured_at, book, "
                 "odds_h, odds_d, odds_a) VALUES (1, 'T-7m', 'pinnacle', "
                 "NULL, 2.5, 3.0)")
    res = update_standings(conn)
    assert res["scored_matches"] == 0


# ---------- compute_prize_distribution ----------

def test_prize_distribution_applies_ladder_to_ranked_participants():
    conn = _schema_conn()
    # 3 participants with known totals
    conn.executemany(
        "INSERT INTO standings (participant, group_points, knockout_points, "
        "futures_points) VALUES (?, ?, ?, ?)",
        [("alice", 100.0, 50.0, 20.0),    # total 170 → 1st
         ("bob",   80.0, 40.0, 10.0),     # total 130 → 2nd
         ("carol", 60.0, 20.0, 5.0)])     # total  85 → 3rd

    dist = compute_prize_distribution(conn, total_pot=1000.0, n_ranked=10)
    assert len(dist) == 3
    assert dist[0]["participant"] == "alice"  and dist[0]["rank"] == 1
    assert dist[1]["participant"] == "bob"    and dist[1]["rank"] == 2
    assert dist[2]["participant"] == "carol"  and dist[2]["rank"] == 3
    # PDF prize ladder applied — first place gets 23% of pot
    assert dist[0]["prize"] == 230.0
    assert dist[1]["prize"] == 150.0


def test_prize_distribution_handles_ties_stably():
    """Tie on total — secondary sort by participant name (stable)."""
    conn = _schema_conn()
    conn.executemany(
        "INSERT INTO standings (participant, group_points, knockout_points, "
        "futures_points) VALUES (?, ?, ?, ?)",
        [("zed",   100.0, 0.0, 0.0),
         ("alice", 100.0, 0.0, 0.0)])
    dist = compute_prize_distribution(conn, total_pot=1000.0)
    # 'alice' sorts before 'zed' alphabetically, same total
    assert dist[0]["participant"] == "alice"
    assert dist[1]["participant"] == "zed"

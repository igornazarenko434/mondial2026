"""Day-9.26: regression pins for the standings rewrite.

Live incident (2026-06-12 morning):
  • Mexico v South Africa finished 2-0 detonator at 21:00 UTC
  • Negev app showed updated standings within minutes (G. Cain 11.3,
    Vaadia 7.8, Igor 1.0, Israeli 0.0, …)
  • But our 📊 morning Telegram + DB rows still showed everyone at 0.0
  • Root cause: Negev's Cloud Function stopped writing the GLOBAL
    `users/{uid}.pointsTotal` field. The app aggregates client-side from
    `bets/`. Our toto_get_standings was reading the (stale, zero)
    global field — hence everyone at 0.

This commit fixes toto_get_standings to aggregate from `bets/` like the
app does. These tests pin the regression so it can't reappear.
"""
from __future__ import annotations
from unittest.mock import patch

import pytest

from integrations import negev_toto_mcp as ntm


@pytest.fixture
def mock_negev(monkeypatch):
    """Mock the Negev MCP reads with a realistic small dataset post-Mexico."""
    users = [
        {"uid": "uid-igor",    "displayName": "Igor",    "role": "player",
         "tournaments": ["t1"]},
        {"uid": "uid-vaadia",  "displayName": "Vaadia",  "role": "player",
         "tournaments": ["t1"]},
        {"uid": "uid-cain",    "displayName": "G. Cain", "role": "player",
         "tournaments": ["t1"]},
        {"uid": "bot_owl",     "displayName": "Owl",     "role": "bot",
         "isBot": True, "tournaments": ["t1"]},
        {"uid": "uid-outside", "displayName": "Outside", "role": "player",
         "tournaments": ["other-t"]},
    ]
    matches = [
        {"apiFixtureId": 9001, "tournamentId": "t1",
         "stage": "Group Stage - 1", "_path": "matches/t1_9001"},
        {"apiFixtureId": 9002, "tournamentId": "t1",
         "stage": "Round of 16",     "_path": "matches/t1_9002"},
    ]
    bets = [
        # Mexico match (group): everyone bet, only some scored
        {"userId": "uid-cain",    "tournamentId": "t1",
         "matchId": "t1_9001", "points": 10.3, "isExactScore": True},
        {"userId": "uid-vaadia",  "tournamentId": "t1",
         "matchId": "t1_9001", "points": 6.8,  "isExactScore": False},
        {"userId": "uid-igor",    "tournamentId": "t1",
         "matchId": "t1_9001", "points": 0.0,  "isExactScore": False},
        # KO match (not yet played for anyone)
        # Outside-tournament bet for Igor — must NOT count
        {"userId": "uid-igor",    "tournamentId": "other-t",
         "matchId": "other-t_8000", "points": 99.0, "isExactScore": True},
    ]
    def _read_all(coll, **_kw):
        if coll == "users":
            return users
        if coll == "matches":
            return matches
        if coll == "bets":
            return bets
        return []
    monkeypatch.setattr(ntm, "_read_all", _read_all)


def test_post_mexico_standings_match_app_for_known_users(mock_negev):
    """The fixture mirrors the actual Negev state post-Mexico. Aggregation
    must produce: G. Cain (10.3, 1 exact), Vaadia (6.8), Igor (0)."""
    rows = ntm.toto_get_standings("t1", include_bots=False)
    by_name = {r["player"]: r for r in rows}
    assert by_name["G. Cain"]["total"] == 10.3
    assert by_name["G. Cain"]["direction"] == 10.3
    assert by_name["G. Cain"]["knockout"] == 0
    assert by_name["G. Cain"]["exactCount"] == 1
    assert by_name["Vaadia"]["total"] == 6.8
    assert by_name["Igor"]["total"] == 0.0
    # G. Cain should be rank 1
    assert rows[0]["player"] == "G. Cain"
    assert rows[0]["rank"] == 1


def test_outside_tournament_bets_never_leak(mock_negev):
    """Igor has a 99-pt bet in 'other-t' that must NEVER bleed into 't1'
    standings (this is the cross-tournament contamination bug that
    Day-9.16's baseline-subtraction was trying to band-aid)."""
    rows = ntm.toto_get_standings("t1")
    igor = next(r for r in rows if r["player"] == "Igor")
    # 99-pt outside bet stays out — Igor's total is just the 0 from match 9001
    assert igor["total"] == 0.0


def test_bots_with_no_bets_in_tournament_show_zero(mock_negev):
    """Day-9.26: with bet-based aggregation, bots that haven't bet in the
    current tournament naturally show 0 — no special-casing needed (the
    bug fixed by removing Day-9.15's bot-override + Day-9.16's baseline)."""
    rows = ntm.toto_get_standings("t1", include_bots=True)
    owl = next(r for r in rows if r["player"] == "Owl")
    assert owl["total"] == 0.0
    assert owl["role"] == "bot"


def test_user_doc_pointsTotal_is_ignored(monkeypatch):
    """The smoking gun: if Negev's user-doc pointsTotal is non-zero but
    NO bets exist, we MUST report 0. Pre-Day-9.26 we would have shown
    the stale global value."""
    users = [
        {"uid": "u1", "displayName": "Stale", "role": "player",
         "tournaments": ["t1"], "pointsTotal": 999,  # stale residue
         "directionPoints": 999, "broadBetPoints": 999,
         "exactScoreCount": 99},
    ]
    def _read_all(coll, **_kw):
        if coll == "users":
            return users
        return []
    monkeypatch.setattr(ntm, "_read_all", _read_all)
    rows = ntm.toto_get_standings("t1")
    # Even with 999 in the global field, no bets = 0 total
    assert rows[0]["total"] == 0.0
    assert rows[0]["direction"] == 0.0
    assert rows[0]["exactCount"] == 0


def test_ko_stage_buckets_separately(monkeypatch):
    """Day-9.26 column split: Round of 16 / Final etc. land in `knockout`,
    'Group Stage - 1' lands in `direction`. The Negev app shows these as
    separate columns; we mirror that."""
    users = [{"uid": "u1", "displayName": "X", "role": "player",
              "tournaments": ["t1"]}]
    matches = [
        {"apiFixtureId": 1, "tournamentId": "t1", "stage": "Group Stage - 1"},
        {"apiFixtureId": 2, "tournamentId": "t1", "stage": "Round of 16"},
        {"apiFixtureId": 3, "tournamentId": "t1", "stage": "Quarter-final"},
        {"apiFixtureId": 4, "tournamentId": "t1", "stage": "Semi-final"},
        {"apiFixtureId": 5, "tournamentId": "t1", "stage": "Final"},
        {"apiFixtureId": 6, "tournamentId": "t1", "stage": "Third place"},
    ]
    bets = [
        {"userId": "u1", "tournamentId": "t1", "matchId": "t1_1", "points": 2.0},
        {"userId": "u1", "tournamentId": "t1", "matchId": "t1_2", "points": 3.0},
        {"userId": "u1", "tournamentId": "t1", "matchId": "t1_3", "points": 4.0},
        {"userId": "u1", "tournamentId": "t1", "matchId": "t1_4", "points": 5.0},
        {"userId": "u1", "tournamentId": "t1", "matchId": "t1_5", "points": 6.0},
        {"userId": "u1", "tournamentId": "t1", "matchId": "t1_6", "points": 7.0},
    ]
    def _read_all(coll, **_kw):
        return {"users": users, "matches": matches, "bets": bets}.get(coll, [])
    monkeypatch.setattr(ntm, "_read_all", _read_all)
    rows = ntm.toto_get_standings("t1")
    r = rows[0]
    assert r["direction"] == 2.0
    assert r["knockout"] == 25.0     # R16(3) + QF(4) + SF(5) + Final(6) + 3rd(7)
    assert r["total"] == 27.0


def test_sync_writes_all_four_categories_to_db(monkeypatch, tmp_path):
    """End-to-end: a Negev row with all 4 fields lands in the standings
    table with 4 distinct columns populated."""
    import sqlite3
    from tools import sync_negev_standings as sns
    here = __import__("os").path.dirname(__import__("os").path.dirname(
        __import__("os").path.abspath(__file__)))
    schema_path = __import__("os").path.join(here, "store", "schema.sql")
    db = sqlite3.connect(str(tmp_path / "test.db"))
    db.row_factory = sqlite3.Row
    with open(schema_path) as f:
        db.executescript(f.read())
    db.commit()
    row = {"player": "Igor", "direction": 6.8, "knockout": 2.0,
            "side": 1.0, "broad": 0.5}
    sns._upsert_standings(db, row, dry=False)
    cur = db.execute("SELECT group_points, knockout_points, side_points, "
                      "futures_points FROM standings WHERE participant='Igor'"
                      ).fetchone()
    assert cur["group_points"] == 6.8
    assert cur["knockout_points"] == 2.0
    assert cur["side_points"] == 1.0
    assert cur["futures_points"] == 0.5

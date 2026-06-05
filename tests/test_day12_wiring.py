"""Completeness/wiring audit for Days 1–2: prove the data actually flows from the
calendar all the way to the per-game card, with detonator + stage intact, and that
init_db's schema matches what ingest/repo/cost use.
"""
import sqlite3
import core.data.football_data as fd
from store import repo
from store.db import init_db
from orchestrator.pipeline import process_match


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE matches (match_id INTEGER PRIMARY KEY, utc_kickoff TEXT,
        local_kickoff TEXT, stage TEXT, grp TEXT, home TEXT, away TEXT, status TEXT,
        home_goals INTEGER, away_goals INTEGER, detonator INTEGER DEFAULT 0)""")
    return conn


def test_detonator_and_stage_flow_calendar_to_card(monkeypatch):
    """Mexico–South Africa (a known detonator) within 60 min must reach build_card
    with detonator=True and stage='Group'."""
    from datetime import datetime, timezone, timedelta
    conn = _conn()
    ko = (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat()
    conn.execute("INSERT INTO matches (match_id,utc_kickoff,stage,home,away,status) "
                 "VALUES (1,?,?,?,?,?)", (ko, "Group", "Mexico", "South Africa", "TIMED"))
    conn.commit()
    assert fd.tag_detonators(conn) >= 1                     # CSV pair → tagged

    up = repo.upcoming_matches(conn)                        # store → scheduler shape
    assert up and up[0]["detonator"] is True and up[0]["stage"] == "Group"

    seen = {}
    import core.delivery as d
    monkeypatch.setattr(d, "deliver_card", lambda c: seen.update(c) or True)
    monkeypatch.setattr(d, "alert", lambda t, b: True)

    def build_card(match):
        # build_card receives the real match dict → detonator must be honoured
        assert match["detonator"] is True and match["stage"] == "Group"
        return {"home": match["home"], "away": match["away"], "stage": match["stage"],
                "detonator": match["detonator"], "pick_direction": "H",
                "pick_exact_score": {"home": 1, "away": 0}, "expected_points": 2.0,
                "model_prob": {"H": .5, "D": .3, "A": .2},
                "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0}}

    res = process_match(up[0], "T-7m", build_card=build_card)
    assert res["status"] == "ok" and seen["detonator"] is True


def test_initdb_schema_supports_ingest_and_cost(tmp_path):
    """init_db's schema must accept an ingest row AND a cost record with duration_ms
    (guards the schema.sql ↔ code drift we just fixed)."""
    db = str(tmp_path / "m.db")
    conn = init_db(db)
    # ingest-shaped row
    conn.execute("""INSERT INTO matches (match_id,utc_kickoff,local_kickoff,stage,grp,
        home,away,status,home_goals,away_goals) VALUES (1,'2026-06-11T19:00:00+00:00',
        '2026-06-11T22:00:00+03:00','Group','Group A','Mexico','South Africa','TIMED',NULL,NULL)""")
    # cost record incl. duration_ms (must not raise 'no column')
    conn.execute("""INSERT INTO api_calls (ts,provider,endpoint,units,tokens,duration_ms,
        est_cost,ok,correlation_id) VALUES ('t','odds_api','odds',1,0,12.3,0,1,'c')""")
    conn.commit()
    assert conn.execute("SELECT detonator FROM matches WHERE match_id=1").fetchone()[0] == 0
    assert conn.execute("SELECT duration_ms FROM api_calls").fetchone()[0] == 12.3

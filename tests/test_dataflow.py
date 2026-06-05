"""End-to-end time-sync: matches table -> repo -> scheduler windows."""
import sqlite3
from datetime import datetime, timezone, timedelta
from store import repo
from schedule.scheduler import jobs_for_match, due_jobs


def _db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE matches (match_id INTEGER PRIMARY KEY, utc_kickoff TEXT,
        local_kickoff TEXT, stage TEXT, grp TEXT, home TEXT, away TEXT, status TEXT,
        home_goals INTEGER, away_goals INTEGER, detonator INTEGER DEFAULT 0)""")
    return conn


def _add(conn, mid, ko_iso, home, away, status="TIMED", det=0, hg=None, ag=None, stage="Group"):
    conn.execute("INSERT INTO matches (match_id,utc_kickoff,stage,home,away,status,home_goals,away_goals,detonator)"
                 " VALUES (?,?,?,?,?,?,?,?,?)",
                 (mid, ko_iso, stage, home, away, status, hg, ag, det))
    conn.commit()


def test_upcoming_excludes_tbd_and_finished():
    conn = _db()
    now = datetime.now(timezone.utc)
    _add(conn, 1, (now + timedelta(hours=2)).isoformat(), "Norway", "France", det=1)
    _add(conn, 2, (now + timedelta(hours=2)).isoformat(), None, None, status="SCHEDULED")  # TBD knockout
    _add(conn, 3, (now - timedelta(hours=2)).isoformat(), "Spain", "Italy", status="FINISHED", hg=2, ag=1)
    _add(conn, 4, (now + timedelta(days=5)).isoformat(), "Brazil", "Ghana")               # beyond horizon
    up = repo.upcoming_matches(conn)
    ids = {m["match_id"] for m in up}
    assert ids == {1}                       # only the real, near, scheduled match
    assert up[0]["detonator"] is True


def test_window_timing_from_kickoff():
    now = datetime.now(timezone.utc)
    def at(mins):  # a match kicking off `mins` from now
        return {"match_id": 1, "utc_kickoff": (now + timedelta(minutes=mins)).isoformat()}
    assert any(j["window"] == "T-7m" for j in due_jobs([at(7)], now))
    assert any(j["window"] == "T-15m" for j in due_jobs([at(15)], now))
    assert any(j["window"] == "T-60m" for j in due_jobs([at(60)], now))
    assert any(j["window"] == "T-24h" for j in due_jobs([at(24 * 60)], now))
    assert due_jobs([at(180)], now) == []   # 3h out: nothing due yet


def test_recent_finished_for_results():
    conn = _db()
    now = datetime.now(timezone.utc)
    _add(conn, 3, (now - timedelta(hours=2)).isoformat(), "Spain", "Italy",
         status="FINISHED", hg=2, ag=1)
    fin = repo.recent_finished(conn)
    assert len(fin) == 1 and fin[0]["home_goals"] == 2   # winner derivable -> who advances

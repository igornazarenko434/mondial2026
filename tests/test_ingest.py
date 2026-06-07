"""Day-1 ingestion proven OFFLINE: parse a realistic football-data response →
rules-stage mapping + team-name normalization + store write + repo read.
No network: requests.get is monkeypatched.
"""
import sqlite3
import core.data.football_data as fd
from store import repo

SAMPLE = {"matches": [
    {"id": 1, "utcDate": "2026-06-11T19:00:00Z", "status": "TIMED",
     "stage": "GROUP_STAGE", "group": "Group A",
     "homeTeam": {"name": "Mexico"}, "awayTeam": {"name": "South Africa"},
     "score": {"fullTime": {"home": None, "away": None}}},
    {"id": 2, "utcDate": "2026-07-05T19:00:00Z", "status": "SCHEDULED",
     "stage": "LAST_16", "group": None,
     # spellings that football-data.org actually emits in the WC feed (Jun 2026):
     "homeTeam": {"name": "Korea Republic"},
     "awayTeam": {"name": "Cape Verde Islands"},
     "score": {"fullTime": {"home": None, "away": None}}},
    {"id": 3, "utcDate": "2026-06-11T16:00:00Z", "status": "FINISHED",
     "stage": "GROUP_STAGE", "group": "Group B",
     "homeTeam": {"name": "Türkiye"}, "awayTeam": {"name": "DR Congo"},
     "score": {"fullTime": {"home": 2, "away": 1}}},
]}


class _Resp:
    def json(self): return SAMPLE
    def raise_for_status(self): pass


def test_parse_maps_stage_and_normalizes_names(monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "dummy")
    monkeypatch.setattr(fd.requests, "get", lambda *a, **k: _Resp())
    rows = fd.fetch_wc_matches()
    by_id = {r["match_id"]: r for r in rows}
    assert by_id[1]["stage"] == "Group" and by_id[1]["home"] == "Mexico"
    assert by_id[2]["stage"] == "R16"                       # LAST_16 -> R16
    assert by_id[2]["home"] == "South Korea"                # Korea Republic normalized
    assert by_id[2]["away"] == "Cape Verde"                 # Cabo Verde normalized
    assert by_id[3]["home"] == "Türkiye" and by_id[3]["away"] == "Congo DR"
    assert by_id[3]["home_goals"] == 2                      # finished score captured
    assert by_id[1]["utc_kickoff"].startswith("2026-06-11")


def test_ingest_writes_store_and_repo_reads(monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "dummy")
    monkeypatch.setattr(fd.requests, "get", lambda *a, **k: _Resp())
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE matches (match_id INTEGER PRIMARY KEY, utc_kickoff TEXT,
        local_kickoff TEXT, stage TEXT, grp TEXT, home TEXT, away TEXT, status TEXT,
        home_goals INTEGER, away_goals INTEGER, detonator INTEGER DEFAULT 0)""")
    n = fd.ingest(conn)
    assert n == 3
    # finished match shows up in recent_finished (who won → who advances)
    fin = {r["match_id"] for r in repo.recent_finished(conn, hours=24 * 400)}
    assert 3 in fin
    # stored stage is the rules stage, ready for score_match
    stage = conn.execute("SELECT stage FROM matches WHERE match_id=2").fetchone()[0]
    assert stage == "R16"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE matches (match_id INTEGER PRIMARY KEY, utc_kickoff TEXT,
        local_kickoff TEXT, stage TEXT, grp TEXT, home TEXT, away TEXT, status TEXT,
        home_goals INTEGER, away_goals INTEGER, detonator INTEGER DEFAULT 0)""")
    return conn


def test_ingest_is_idempotent(monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "dummy")
    monkeypatch.setattr(fd.requests, "get", lambda *a, **k: _Resp())
    conn = _conn()
    fd.ingest(conn); fd.ingest(conn)                        # twice
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 3   # no dupes


def test_refresh_tags_detonators_order_independent(monkeypatch):
    # SAMPLE match 1 is Mexico vs South Africa (a known detonator) — tag it even
    # though the CSV lists the pair; order must not matter.
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "dummy")
    monkeypatch.setattr(fd.requests, "get", lambda *a, **k: _Resp())
    conn = _conn()
    res = fd.refresh(conn)
    assert res["matches"] == 3 and res["detonators"] >= 1
    det = conn.execute("SELECT detonator FROM matches WHERE match_id=1").fetchone()[0]
    assert det == 1                                         # Mexico–South Africa tagged


def test_detonator_tag_survives_reingest(monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "dummy")
    monkeypatch.setattr(fd.requests, "get", lambda *a, **k: _Resp())
    conn = _conn()
    fd.refresh(conn)
    fd.ingest(conn)                                         # re-ingest must not clear tags
    assert conn.execute("SELECT detonator FROM matches WHERE match_id=1").fetchone()[0] == 1


def test_fetch_skips_match_without_kickoff(monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "dummy")
    bad = {"matches": [{"id": 9, "utcDate": None, "stage": "GROUP_STAGE",
                        "homeTeam": {"name": "X"}, "awayTeam": {"name": "Y"},
                        "score": {"fullTime": {}}}]}
    monkeypatch.setattr(fd.requests, "get",
                        lambda *a, **k: type("R", (), {"json": lambda s: bad,
                                                       "raise_for_status": lambda s: None})())
    assert fd.fetch_wc_matches() == []                      # skipped, no crash


def test_fetch_strips_group_prefix_to_match_canonical_csv(monkeypatch):
    """football-data.org returns group="GROUP_A"; we store "A" so it matches
    data/wc2026_groups.csv. Regression pin for the audit-discovered mismatch."""
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "dummy")
    payload = {"matches": [
        {"id": 100, "utcDate": "2026-06-11T19:00:00Z", "stage": "GROUP_STAGE",
         "group": "GROUP_A",
         "homeTeam": {"name": "Mexico"}, "awayTeam": {"name": "South Africa"},
         "status": "TIMED", "score": {"fullTime": {}}},
        {"id": 101, "utcDate": "2026-06-12T19:00:00Z", "stage": "GROUP_STAGE",
         "group": "GROUP_L",
         "homeTeam": {"name": "England"}, "awayTeam": {"name": "Croatia"},
         "status": "TIMED", "score": {"fullTime": {}}},
        # Defensive: an unusual format (no prefix) should pass through untouched
        {"id": 102, "utcDate": "2026-06-12T22:00:00Z", "stage": "GROUP_STAGE",
         "group": "Group A",
         "homeTeam": {"name": "X"}, "awayTeam": {"name": "Y"},
         "status": "TIMED", "score": {"fullTime": {}}},
    ]}
    monkeypatch.setattr(fd.requests, "get",
                        lambda *a, **k: type("R", (), {"json": lambda s: payload,
                                                       "raise_for_status": lambda s: None})())
    rows = fd.fetch_wc_matches()
    by_id = {r["match_id"]: r for r in rows}
    assert by_id[100]["group"] == "A"               # stripped
    assert by_id[101]["group"] == "L"               # stripped
    assert by_id[102]["group"] == "Group A"         # left alone (different format)

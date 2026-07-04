"""Day-9.35 — KO penalty-shootout aggregate-score bug + fix.

Root cause (surfaced 2026-07-04 via `tools/verify_scoring_sync.py`):
  football-data.org's `score.fullTime` field for a match decided on
  penalties contains the AGGREGATE tally (regulation + extra time +
  shootout goals). Our ingest was storing this verbatim in
  `matches.home_goals` / `matches.away_goals`. Result: 3 R32 matches
  were persisted with fake scores like 4-5, 3-4, 3-5 instead of the
  true 120-minute results (all 1-1, decided on pens).

Impact:
  * `score_match()` on stored rows computed direction A instead of D
    for these matches — penalty-winner bonus lost.
  * `verify_scoring_sync.py` reported a mismatch between our stored
    results and Negev's authoritative 120' scores.
  * Any backtest / Monte Carlo recalibration reading the historical
    `matches` table would inherit the corruption.

Fix (this commit):
  1. Add nullable `penalty_home` / `penalty_away` columns to `matches`
     (idempotent ALTER via `store.db._apply_pending_migrations`).
  2. New `_extract_120min_and_pens()` helper in
     `core/data/football_data.py` subtracts shootout goals from
     `fullTime` when `duration == "PENALTY_SHOOTOUT"`, leaving the
     true 120-minute result in `home_goals`/`away_goals`.
  3. Extend the UPSERT and the Negev sync to write penalty columns.

Every test below mirrors one of the concrete edge cases identified
during design so a future refactor can't silently regress.
"""
from __future__ import annotations
import sqlite3

import pytest

from core.data.football_data import _extract_120min_and_pens, ingest
from store.db import _apply_pending_migrations


# ──────────────────────────────────────────────────────────────────────────
# 1. _extract_120min_and_pens() — pure function, edge-case coverage
# ──────────────────────────────────────────────────────────────────────────

def test_extract_regulation_decided_uses_fullTime_verbatim():
    """Match ends in regulation: fullTime == regularTime, no ET, no pens."""
    score = {
        "winner": "HOME_TEAM",
        "duration": "REGULAR",
        "fullTime":    {"home": 2, "away": 1},
        "regularTime": {"home": 2, "away": 1},
        "extraTime":   {"home": 0, "away": 0},
        "penalties":   None,
    }
    assert _extract_120min_and_pens(score) == (2, 1, None, None)


def test_extract_extra_time_decided_uses_fullTime_verbatim():
    """Match decided in ET: fullTime = regulation + ET, still no pens."""
    score = {
        "winner": "AWAY_TEAM",
        "duration": "EXTRA_TIME",
        "fullTime":    {"home": 1, "away": 2},
        "regularTime": {"home": 1, "away": 1},
        "extraTime":   {"home": 0, "away": 1},
        "penalties":   None,
    }
    assert _extract_120min_and_pens(score) == (1, 2, None, None)


def test_extract_penalty_shootout_returns_120min_not_aggregate():
    """The bug incident: fullTime 4-5, pens 3-4 → 120' result 1-1."""
    score = {
        "winner": "AWAY_TEAM",
        "duration": "PENALTY_SHOOTOUT",
        "fullTime":    {"home": 4, "away": 5},
        "regularTime": {"home": 1, "away": 1},
        "extraTime":   {"home": 0, "away": 0},
        "penalties":   {"home": 3, "away": 4},
    }
    assert _extract_120min_and_pens(score) == (1, 1, 3, 4)


def test_extract_shootout_after_extra_time_still_correct():
    """Match: reg 1-1, ET 1-1 (still tied), pens 5-4 → 120' = 2-2, pens 5-4."""
    score = {
        "winner": "HOME_TEAM",
        "duration": "PENALTY_SHOOTOUT",
        "fullTime":    {"home": 7, "away": 6},   # 2+5, 2+4
        "regularTime": {"home": 1, "away": 1},
        "extraTime":   {"home": 1, "away": 1},
        "penalties":   {"home": 5, "away": 4},
    }
    assert _extract_120min_and_pens(score) == (2, 2, 5, 4)


def test_extract_shootout_from_zero_zero_regulation():
    """0-0 through 120' + pens 4-3 → stored as 0-0 with pens 4-3."""
    score = {
        "duration": "PENALTY_SHOOTOUT",
        "fullTime":    {"home": 4, "away": 3},
        "regularTime": {"home": 0, "away": 0},
        "extraTime":   {"home": 0, "away": 0},
        "penalties":   {"home": 4, "away": 3},
    }
    assert _extract_120min_and_pens(score) == (0, 0, 4, 3)


def test_extract_sudden_death_long_shootout():
    """Rare but legal: shootout goes into sudden-death (e.g. 7-6)."""
    score = {
        "duration": "PENALTY_SHOOTOUT",
        "fullTime":    {"home": 8, "away": 7},   # 1+7, 1+6
        "regularTime": {"home": 1, "away": 1},
        "extraTime":   {"home": 0, "away": 0},
        "penalties":   {"home": 7, "away": 6},
    }
    assert _extract_120min_and_pens(score) == (1, 1, 7, 6)


def test_extract_scheduled_match_returns_all_nulls():
    """SCHEDULED/TIMED match: score fields absent."""
    assert _extract_120min_and_pens({}) == (None, None, None, None)


def test_extract_in_play_match_partial_data():
    """IN_PLAY match: fullTime populated but no regulation-end yet."""
    score = {
        "duration": "REGULAR",
        "fullTime":  {"home": 1, "away": 0},
        "halfTime":  {"home": 0, "away": 0},
    }
    assert _extract_120min_and_pens(score) == (1, 0, None, None)


def test_extract_malformed_pens_falls_back_to_fullTime():
    """Corrupted API response: duration=PEN but pens dict is broken.
    We must NOT crash the ingest tick — fall back to fullTime as-is
    and skip the pen columns (defensive)."""
    score = {
        "duration": "PENALTY_SHOOTOUT",
        "fullTime": {"home": 4, "away": 5},
        "penalties": {"home": "oops", "away": None},
    }
    result = _extract_120min_and_pens(score)
    # Should not raise; degrades to fullTime + no pens.
    assert result[2] is None  # penalty_home cleared
    assert result[3] is None  # penalty_away cleared


def test_extract_none_score_object():
    """Robustness: `score` key returned as None or missing entirely."""
    assert _extract_120min_and_pens(None) == (None, None, None, None)  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────────────────
# 2. Schema migration — idempotent + adds both columns
# ──────────────────────────────────────────────────────────────────────────

def _make_pre_day935_matches_table(conn: sqlite3.Connection) -> None:
    """Recreate the OLD schema without penalty_home/penalty_away."""
    conn.executescript("""
        DROP TABLE IF EXISTS matches;
        CREATE TABLE matches (
            match_id    INTEGER PRIMARY KEY,
            utc_kickoff TEXT,
            local_kickoff TEXT,
            stage       TEXT,
            grp         TEXT,
            home        TEXT,
            away        TEXT,
            status      TEXT,
            home_goals  INTEGER,
            away_goals  INTEGER,
            detonator   INTEGER DEFAULT 0
        )
    """)
    conn.commit()


def test_migration_adds_penalty_columns_when_missing(tmp_path):
    dbfile = tmp_path / "old.db"
    conn = sqlite3.connect(str(dbfile))
    _make_pre_day935_matches_table(conn)

    before = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
    assert "penalty_home" not in before
    assert "penalty_away" not in before

    _apply_pending_migrations(conn)

    after = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
    assert "penalty_home" in after
    assert "penalty_away" in after


def test_migration_is_idempotent(tmp_path):
    """Running migrations twice must not error and must not double-add."""
    dbfile = tmp_path / "twice.db"
    conn = sqlite3.connect(str(dbfile))
    _make_pre_day935_matches_table(conn)

    _apply_pending_migrations(conn)
    # Second call must be a no-op — no OperationalError from duplicate ADD.
    _apply_pending_migrations(conn)

    cols = [r[1] for r in conn.execute("PRAGMA table_info(matches)")]
    assert cols.count("penalty_home") == 1
    assert cols.count("penalty_away") == 1


def test_migration_on_fresh_schema_is_noop(tmp_path):
    """New-install path: schema.sql already defines the columns; migration
    must recognise that and skip the ALTER."""
    from store.db import init_db
    dbfile = tmp_path / "fresh.db"
    conn = init_db(str(dbfile))
    _apply_pending_migrations(conn)  # second call — should be safe
    cols = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
    assert {"penalty_home", "penalty_away"} <= cols


# ──────────────────────────────────────────────────────────────────────────
# 3. Ingest UPSERT — end-to-end with the new columns
# ──────────────────────────────────────────────────────────────────────────

def _fake_fetch(rows_pack):
    """Return a fake `fetch_wc_matches` output for a list of raw fd.org matches."""
    from core.data import football_data as fd
    fd._original_fetch = fd.fetch_wc_matches   # keep for cleanup

    def _fake():
        from core.data.football_data import _extract_120min_and_pens
        from core.data.teams import normalize
        from core.data.football_data import to_rules_stage, _local_iso
        from datetime import datetime, timezone
        out = []
        for m in rows_pack:
            hg, ag, ph, pa = _extract_120min_and_pens(m.get("score") or {})
            utc = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
            out.append({
                "match_id": m["id"],
                "utc_kickoff": utc.astimezone(timezone.utc).isoformat(),
                "local_kickoff": _local_iso(utc),
                "stage": to_rules_stage(m.get("stage")),
                "group": (m.get("group") or "").replace("GROUP_", "") or None,
                "home": normalize((m.get("homeTeam") or {}).get("name")),
                "away": normalize((m.get("awayTeam") or {}).get("name")),
                "status": m.get("status"),
                "home_goals": hg,
                "away_goals": ag,
                "penalty_home": ph,
                "penalty_away": pa,
            })
        return out
    return _fake


@pytest.fixture
def db(tmp_path):
    from store.db import init_db
    dbfile = tmp_path / "ingest.db"
    conn = init_db(str(dbfile))
    yield conn
    conn.close()


def test_ingest_pens_match_populates_all_four_score_fields(db, monkeypatch):
    """Full ingest cycle for a PENALTY_SHOOTOUT match — verify all four
    stored columns match the expected 120' + shootout split."""
    match = {
        "id": 999001,
        "utcDate": "2026-06-29T20:30:00Z",
        "stage": "LAST_32",
        "group": None,
        "homeTeam": {"name": "Germany"},
        "awayTeam": {"name": "Paraguay"},
        "status": "FINISHED",
        "score": {
            "winner": "AWAY_TEAM",
            "duration": "PENALTY_SHOOTOUT",
            "fullTime":    {"home": 4, "away": 5},
            "regularTime": {"home": 1, "away": 1},
            "extraTime":   {"home": 0, "away": 0},
            "penalties":   {"home": 3, "away": 4},
        },
    }
    from core.data import football_data as fd
    monkeypatch.setattr(fd, "fetch_wc_matches", _fake_fetch([match]))

    ingest(db)

    row = db.execute(
        "SELECT home_goals, away_goals, penalty_home, penalty_away, status "
        "FROM matches WHERE match_id = ?", (match["id"],)
    ).fetchone()
    assert row is not None
    assert row[0] == 1, "home_goals should be regulation-only (1), not aggregate (4)"
    assert row[1] == 1, "away_goals should be regulation-only (1), not aggregate (5)"
    assert row[2] == 3, "penalty_home should be the shootout tally"
    assert row[3] == 4, "penalty_away should be the shootout tally"
    assert row[4] == "FINISHED"


def test_ingest_group_match_leaves_penalty_columns_null(db, monkeypatch):
    """Group-stage games never go to pens — the pen columns must stay NULL."""
    match = {
        "id": 999002,
        "utcDate": "2026-06-11T18:00:00Z",
        "stage": "GROUP_STAGE",
        "group": "GROUP_A",
        "homeTeam": {"name": "Mexico"},
        "awayTeam": {"name": "South Africa"},
        "status": "FINISHED",
        "score": {
            "winner": "HOME_TEAM",
            "duration": "REGULAR",
            "fullTime":    {"home": 2, "away": 0},
            "regularTime": {"home": 2, "away": 0},
            "extraTime":   {"home": 0, "away": 0},
            "penalties":   None,
        },
    }
    from core.data import football_data as fd
    monkeypatch.setattr(fd, "fetch_wc_matches", _fake_fetch([match]))

    ingest(db)

    row = db.execute(
        "SELECT home_goals, away_goals, penalty_home, penalty_away "
        "FROM matches WHERE match_id = ?", (match["id"],)
    ).fetchone()
    assert (row[0], row[1], row[2], row[3]) == (2, 0, None, None)


def test_ingest_second_pass_self_heals_pre_fix_wrong_row(db, monkeypatch):
    """A row already in the DB with the OLD wrong aggregate score must be
    OVERWRITTEN to the correct 120' result on the next ingest tick.

    This is the auto-heal path — production has 3 R32 rows in this state
    right now. The COALESCE `excluded.home_goals` (=1 correct) prevails
    over existing home_goals (=4 wrong) because 1 is not NULL."""
    # Seed the row with the WRONG pre-fix aggregate values.
    db.execute("INSERT INTO matches (match_id, home, away, home_goals, away_goals, status, stage) "
               "VALUES (999003, 'Germany', 'Paraguay', 4, 5, 'FINISHED', 'R32')")
    db.commit()

    match = {
        "id": 999003,
        "utcDate": "2026-06-29T20:30:00Z",
        "stage": "LAST_32",
        "group": None,
        "homeTeam": {"name": "Germany"},
        "awayTeam": {"name": "Paraguay"},
        "status": "FINISHED",
        "score": {
            "duration": "PENALTY_SHOOTOUT",
            "fullTime":    {"home": 4, "away": 5},
            "regularTime": {"home": 1, "away": 1},
            "extraTime":   {"home": 0, "away": 0},
            "penalties":   {"home": 3, "away": 4},
        },
    }
    from core.data import football_data as fd
    monkeypatch.setattr(fd, "fetch_wc_matches", _fake_fetch([match]))

    ingest(db)

    row = db.execute(
        "SELECT home_goals, away_goals, penalty_home, penalty_away "
        "FROM matches WHERE match_id = 999003"
    ).fetchone()
    assert row[0] == 1, "self-heal: home_goals must be overwritten 4 → 1"
    assert row[1] == 1, "self-heal: away_goals must be overwritten 5 → 1"
    assert row[2] == 3
    assert row[3] == 4


def test_ingest_null_score_never_erases_existing_pens_columns(db, monkeypatch):
    """Bracket-transition safety (Day-9.33 pattern): if football-data
    briefly returns NULL for penalty fields, our COALESCE must preserve
    the already-populated values."""
    # Seed a correctly-populated row.
    db.execute(
        "INSERT INTO matches (match_id, home, away, home_goals, away_goals, "
        "                     status, stage, penalty_home, penalty_away) "
        "VALUES (999004, 'Netherlands', 'Morocco', 1, 1, 'FINISHED', 'R32', 2, 3)"
    )
    db.commit()

    # Simulate a transient bad response with NULL fullTime + no pens.
    match = {
        "id": 999004,
        "utcDate": "2026-06-30T04:00:00Z",
        "stage": "LAST_32",
        "group": None,
        "homeTeam": {"name": "Netherlands"},
        "awayTeam": {"name": "Morocco"},
        "status": "FINISHED",
        "score": {"fullTime": {"home": None, "away": None}},
    }
    from core.data import football_data as fd
    monkeypatch.setattr(fd, "fetch_wc_matches", _fake_fetch([match]))

    ingest(db)

    row = db.execute(
        "SELECT home_goals, away_goals, penalty_home, penalty_away "
        "FROM matches WHERE match_id = 999004"
    ).fetchone()
    assert (row[0], row[1], row[2], row[3]) == (1, 1, 2, 3), \
        "transient NULL from API must not erase populated pen columns"


# ──────────────────────────────────────────────────────────────────────────
# 4. Negev sync path — also populates pen columns
# ──────────────────────────────────────────────────────────────────────────

def test_negev_sync_writes_pen_columns_from_scorePenalty_fields(db, monkeypatch):
    """`sync_match_results` must copy Negev's scorePenaltyHome/Away into
    our penalty_home/penalty_away when the match is a PEN result."""
    from tools.sync_negev_standings import sync_match_results

    # Seed the target row (as if football_data ingested it earlier).
    db.execute(
        "INSERT INTO matches (match_id, home, away, home_goals, away_goals, "
        "                     status, stage) "
        "VALUES (999005, 'Australia', 'Egypt', 1, 1, 'SCHEDULED', 'R32')"
    )
    db.commit()

    # Fake ntm module returning one PEN match.
    class _FakeNtm:
        @staticmethod
        def toto_get_matches(*, tournament_id, limit):
            return [{
                "home": "Australia",
                "away": "Egypt",
                "status": "PEN",
                "scoreFullTimeHome": 1,
                "scoreFullTimeAway": 1,
                "scorePenaltyHome": 2,
                "scorePenaltyAway": 4,
            }]

    n = sync_match_results("tid", conn=db, ntm=_FakeNtm(), dry=False)
    assert n == 1

    row = db.execute(
        "SELECT home_goals, away_goals, penalty_home, penalty_away, status "
        "FROM matches WHERE match_id = 999005"
    ).fetchone()
    assert (row[0], row[1], row[2], row[3], row[4]) == (1, 1, 2, 4, "FINISHED")


def test_negev_sync_leaves_pen_columns_null_for_non_pen_match(db, monkeypatch):
    """Regulation-decided match: sync must NOT populate pen columns."""
    from tools.sync_negev_standings import sync_match_results

    db.execute(
        "INSERT INTO matches (match_id, home, away, home_goals, away_goals, "
        "                     status, stage) "
        "VALUES (999006, 'Brazil', 'Japan', NULL, NULL, 'SCHEDULED', 'R32')"
    )
    db.commit()

    class _FakeNtm:
        @staticmethod
        def toto_get_matches(*, tournament_id, limit):
            return [{
                "home": "Brazil",
                "away": "Japan",
                "status": "FT",
                "scoreFullTimeHome": 2,
                "scoreFullTimeAway": 1,
                # No penalty fields — regulation win
            }]

    sync_match_results("tid", conn=db, ntm=_FakeNtm(), dry=False)

    row = db.execute(
        "SELECT home_goals, away_goals, penalty_home, penalty_away "
        "FROM matches WHERE match_id = 999006"
    ).fetchone()
    assert (row[0], row[1]) == (2, 1)
    assert row[2] is None and row[3] is None


# ──────────────────────────────────────────────────────────────────────────
# 5. score_match integration — the direction bug that motivated the fix
# ──────────────────────────────────────────────────────────────────────────

def test_score_match_direction_correct_with_120min_score():
    """The whole point of the fix: with the CORRECT 120' score (1-1), a
    'D 1-1' prediction for a PEN match now gets the exact-score bonus.
    Pre-fix, direction was 'A' (Paraguay won 4-5) and the D prediction
    scored 0 — silently wrong."""
    from core.scoring.engine import score_match, direction

    # Direction of 1-1 = "D" (draw). With 120' score correctly stored,
    # a pick of D-1-1 wins the exact-score cell.
    assert direction(1, 1) == "D"
    odds = {"H": 1.30, "D": 5.25, "A": 10.00}
    pts = score_match("R32", 1, 1, 1, 1, odds)   # KO, 1-1 pred, 1-1 actual
    # KO base × draw multiplier × draw odds
    # From rules pdf + our config: R32 uses ko table; 1-1 = 3.0 mult; base 1.5
    # Actually per test_scoring.py: score_match uses BASE_POINTS[ko]=1.5 for
    # right-direction, multiplied by exact_multiplier when scored exactly.
    # For KO 1-1 exact match, expected = 3.0 * odds(D) = 3.0 * 5.25 = 15.75
    assert pts > 0.0, "1-1 exact-score bonus lost pre-fix"
    assert abs(pts - 3.0 * 5.25) < 0.001


def test_score_match_wrong_direction_when_using_pre_fix_aggregate():
    """Regression pin: if we ever accidentally revert to storing the
    aggregate score, this test shows the concrete harm: predicting 1-1
    (draw) against a match football-data returned as 4-5 aggregate would
    compute direction=A and award ZERO points."""
    from core.scoring.engine import score_match
    odds = {"H": 1.30, "D": 5.25, "A": 10.00}
    # Actual stored as PRE-FIX aggregate 4-5 (the bug) → direction A
    pts_buggy = score_match("R32", 1, 1, 4, 5, odds)  # pred D, actual A
    assert pts_buggy == 0.0, "pre-fix behaviour: D pick vs 4-5 = 0 (wrong)"

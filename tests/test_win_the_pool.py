"""Day-9.5 win-the-pool layer — end-to-end audit.

Pins the four pieces that make `STRATEGY_TILT > 0` actually do something:
  1. standings_context arithmetic (no double-reset bug; no-op safely)
  2. standings_set CLI tool (set / list / import / remove)
  3. SchedulerDaemon loads + passes strategy_context per dispatch
  4. recommend_to_win unchanged when context/tilt missing (regression)
"""
from __future__ import annotations
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from io import StringIO
from unittest.mock import patch

import pytest

from store import repo
from store.db import connect, init_db
from core.scoring.standings_writer import update_standings
from core.scoring.engine import apply_group_reset
from core.decision.strategy import recommend_to_win, risk_pressure
from schedule.runner import SchedulerDaemon
from tools import standings_set


# ─────────────── helpers ───────────────

class _ConnProxy:
    """Wraps a sqlite3.Connection, delegates everything except .close() which
    is neutered. Python 3.14 made Connection.close read-only on the real
    object, so we can't just `c.close = noop`."""
    def __init__(self, inner):
        self._inner = inner
    def __getattr__(self, name):
        return getattr(self._inner, name)
    def close(self):
        pass                                       # standings_set.main() calls this; ignore


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    with open("store/schema.sql") as f:
        c.executescript(f.read())
    return _ConnProxy(c)


def _insert_standing(conn, participant, group=0.0, ko=0.0, futures=0.0):
    conn.execute(
        "INSERT INTO standings (participant, group_points, knockout_points, "
        "futures_points) VALUES (?, ?, ?, ?)",
        (participant, group, ko, futures))
    conn.commit()


def _silence_delivery(monkeypatch):
    import core.delivery as d
    monkeypatch.setattr(d, "deliver_card", lambda c: True)
    monkeypatch.setattr(d, "alert", lambda t, b: True)


# ─────────────────── Piece 2: standings_context bug fixes ───────────────────

def test_standings_context_returns_none_when_empty(conn):
    """No rows → no-op (strategy receives None → returns pure-EV pick)."""
    assert repo.standings_context(conn, me="Igor") is None


def test_standings_context_returns_none_when_only_one_participant(conn):
    """One row means no leader to compare against → safe no-op."""
    _insert_standing(conn, "Igor", group=10)
    assert repo.standings_context(conn, me="Igor") is None


def test_standings_context_returns_none_when_me_is_none(conn):
    """Without a participant identity we can't compute the gap. Pre-fix this
    silently fell back to the LEADER's total as your_points (gap always 0).
    Now it correctly no-ops."""
    _insert_standing(conn, "Alice", group=30)
    _insert_standing(conn, "Bob",   group=20)
    assert repo.standings_context(conn, me=None) is None


def test_standings_context_returns_none_when_me_not_in_standings(conn):
    """Typo'd MY_PARTICIPANT in .env shouldn't silently use someone else's totals."""
    _insert_standing(conn, "Alice", group=30)
    _insert_standing(conn, "Bob",   group=20)
    assert repo.standings_context(conn, me="Igor") is None


def test_standings_context_computes_gap_correctly(conn):
    """Happy path: 3-participant standings, me=Igor in 3rd.
    your_points should equal Igor's stored total — NOT discounted by 0.85."""
    _insert_standing(conn, "Alice", group=40, ko=0,    futures=0)   # leader 40
    _insert_standing(conn, "Bob",   group=35, ko=0,    futures=2)   # second 37
    _insert_standing(conn, "Igor",  group=30, ko=0,    futures=5)   # me 35
    ctx = repo.standings_context(conn, me="Igor")
    assert ctx["your_points"] == 35.0           # raw sum, no 0.85
    assert ctx["leader_points"] == 40.0
    assert ctx["second_points"] == 37.0


def test_standings_context_no_double_reset_post_knockout(conn):
    """The bug we fixed: when update_standings has already applied the §14
    -15 % reset to group_points (because KO matches scored), the reader
    must NOT apply 0.85 again. Verifies the stored value flows through
    untouched."""
    # Simulate post-KO state: Igor's raw group total was 20, writer applied
    # the 0.85 reset → 17 in the DB.
    raw_group = 20.0
    stored_group = apply_group_reset(raw_group)          # = 17.0
    _insert_standing(conn, "Alice", group=stored_group + 3)         # leader = 20
    _insert_standing(conn, "Igor",  group=stored_group, ko=2)       # me = 19
    ctx = repo.standings_context(conn, me="Igor")
    # If the bug were still there: 17 * 0.85 + 2 = 16.45 ≠ 19
    assert ctx["your_points"] == 19.0
    assert ctx["leader_points"] == 20.0


def test_standings_context_games_left_reflects_finished_matches(conn):
    _insert_standing(conn, "Alice", group=10)
    _insert_standing(conn, "Igor", group=8)
    ko_future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO matches (match_id, utc_kickoff, stage, home, away, status) "
        "VALUES (1, ?, 'Group', 'A', 'B', 'SCHEDULED')", (ko_future,))
    conn.execute(
        "INSERT INTO matches (match_id, utc_kickoff, stage, home, away, status) "
        "VALUES (2, ?, 'Group', 'C', 'D', 'SCHEDULED')", (ko_future,))
    conn.execute(
        "INSERT INTO matches (match_id, utc_kickoff, stage, home, away, status, home_goals, away_goals) "
        "VALUES (3, ?, 'Group', 'E', 'F', 'FINISHED', 1, 0)", (ko_future,))
    conn.commit()
    ctx = repo.standings_context(conn, me="Igor")
    assert ctx["games_left"] == 2                          # 2 not-finished


# ─────────────────── Piece 1: standings_set CLI ───────────────────

def test_cli_list_empty_prints_hint(conn, monkeypatch, capsys):
    monkeypatch.setattr(standings_set, "connect", lambda *a, **k: conn)
    standings_set.main(["list"])
    out = capsys.readouterr().out
    assert "no standings entered yet" in out
    assert "set NAME" in out                    # tells user how to add one


def test_cli_set_one_then_list(conn, monkeypatch, capsys):
    monkeypatch.setattr(standings_set, "connect", lambda *a, **k: conn)
    standings_set.main(["set", "Igor", "--group", "24.5", "--ko", "0", "--futures", "4.2"])
    out = capsys.readouterr().out
    assert "Igor" in out and "24.50" in out and "4.20" in out
    rows = conn.execute("SELECT participant, group_points, futures_points FROM standings").fetchall()
    assert dict(rows[0]) == {"participant": "Igor", "group_points": 24.5, "futures_points": 4.2}


def test_cli_set_updates_existing_row(conn, monkeypatch, capsys):
    """ON CONFLICT: re-running set with the same name UPDATES, doesn't duplicate."""
    monkeypatch.setattr(standings_set, "connect", lambda *a, **k: conn)
    standings_set.main(["set", "Igor", "--group", "10", "--ko", "0", "--futures", "0"])
    standings_set.main(["set", "Igor", "--group", "25", "--ko", "5", "--futures", "0"])
    row = conn.execute("SELECT * FROM standings WHERE participant='Igor'").fetchone()
    assert (row["group_points"], row["knockout_points"]) == (25.0, 5.0)
    assert conn.execute("SELECT COUNT(*) FROM standings").fetchone()[0] == 1


def test_cli_set_missing_argument_errors(conn, monkeypatch):
    monkeypatch.setattr(standings_set, "connect", lambda *a, **k: conn)
    # --futures left out: argparse will treat it as None → cmd_set returns 2
    rc = standings_set.main(["set", "Igor", "--group", "10", "--ko", "0"])
    assert rc == 2


def test_cli_remove(conn, monkeypatch, capsys):
    monkeypatch.setattr(standings_set, "connect", lambda *a, **k: conn)
    _insert_standing(conn, "Bob", group=12)
    standings_set.main(["remove", "Bob"])
    out = capsys.readouterr().out
    assert "removed Bob" in out
    assert conn.execute("SELECT COUNT(*) FROM standings WHERE participant='Bob'").fetchone()[0] == 0


def test_cli_remove_nonexistent_returns_nonzero(conn, monkeypatch, capsys):
    monkeypatch.setattr(standings_set, "connect", lambda *a, **k: conn)
    rc = standings_set.main(["remove", "Ghost"])
    assert rc == 1
    assert "not found" in capsys.readouterr().out


def test_cli_import_json(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(standings_set, "connect", lambda *a, **k: conn)
    path = tmp_path / "friends.json"
    path.write_text(json.dumps([
        {"participant": "Alice", "group_points": 30, "knockout_points": 5,  "futures_points": 0},
        {"participant": "Bob",   "group_points": 25, "knockout_points": 0,  "futures_points": 7},
        {"participant": "Igor",  "group_points": 22, "knockout_points": 0,  "futures_points": 4},
    ]))
    standings_set.main(["import", str(path)])
    rows = conn.execute("SELECT participant, group_points, futures_points FROM standings ORDER BY participant").fetchall()
    assert [r["participant"] for r in rows] == ["Alice", "Bob", "Igor"]
    assert {r["participant"]: r["group_points"] for r in rows} == {"Alice": 30, "Bob": 25, "Igor": 22}


def test_cli_import_invalid_rows_skipped_with_warning(conn, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(standings_set, "connect", lambda *a, **k: conn)
    path = tmp_path / "friends.json"
    path.write_text(json.dumps([
        {"participant": "Alice", "group_points": 30, "knockout_points": 0, "futures_points": 0},
        {"name_typo": "Bob", "group_points": 25},                             # missing 'participant'
        {"participant": "Igor", "group_points": "not-a-number"},               # wrong type
    ]))
    standings_set.main(["import", str(path)])
    out = capsys.readouterr().out
    # 1 success, 2 skipped → message reports 1/3
    assert "1/3" in out


def test_cli_import_nonexistent_file_errors(conn, monkeypatch):
    monkeypatch.setattr(standings_set, "connect", lambda *a, **k: conn)
    rc = standings_set.main(["import", "/nonexistent/path.json"])
    assert rc == 2


def test_cli_import_invalid_json_errors(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(standings_set, "connect", lambda *a, **k: conn)
    path = tmp_path / "broken.json"
    path.write_text("{ not valid json")
    rc = standings_set.main(["import", str(path)])
    assert rc == 2


def test_cli_list_marks_you_via_my_participant(conn, monkeypatch, capsys):
    monkeypatch.setattr(standings_set, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    _insert_standing(conn, "Alice", group=10)
    _insert_standing(conn, "Igor", group=8)
    standings_set.main(["list"])
    out = capsys.readouterr().out
    # ← you marker appears on the Igor row
    igor_line = next(line for line in out.split("\n") if "Igor" in line)
    assert "← you" in igor_line


def test_cli_list_warns_when_my_participant_not_in_standings(conn, monkeypatch, capsys):
    monkeypatch.setattr(standings_set, "connect", lambda *a, **k: conn)
    monkeypatch.setenv("MY_PARTICIPANT", "Igor")
    _insert_standing(conn, "Alice", group=10)
    _insert_standing(conn, "Bob", group=8)
    standings_set.main(["list"])
    out = capsys.readouterr().out
    assert "strategy layer will no-op" in out


# ─────────────────── Piece 4: SchedulerDaemon → process_match wiring ───────────────────

def _match(mid, mins_to_ko, stage="Group"):
    ko = datetime.now(timezone.utc) + timedelta(minutes=mins_to_ko)
    return {"match_id": mid, "utc_kickoff": ko.isoformat(),
            "home": f"H{mid}", "away": f"A{mid}", "stage": stage}


def test_daemon_passes_strategy_context_to_process_match(monkeypatch):
    """The bug we fixed: daemon's _run_job called process_match WITHOUT the
    context/tilt parameters, so the strategy layer was unreachable. Now wired."""
    _silence_delivery(monkeypatch)
    seen = {}
    fake_ctx = {"your_points": 30, "leader_points": 50,
                "second_points": 35, "games_left": 8}

    def fake_process_match(match, window, build_card, *,
                            max_attempts=3, strategy_context=None,
                            strategy_tilt=None):
        seen["context"] = strategy_context
        seen["tilt"] = strategy_tilt
        return {"status": "ok", "match_id": match["match_id"], "window": window,
                "delivered": True, "card": {}}

    monkeypatch.setattr("schedule.runner.process_match", fake_process_match)
    matches = [_match(81001, 7)]
    daemon = SchedulerDaemon(
        lambda: matches, lambda m: {},
        strategy_context_fn=lambda: fake_ctx, strategy_tilt=0.4)
    daemon.tick()
    daemon.pool.shutdown(wait=True)
    assert seen["context"] == fake_ctx
    assert seen["tilt"] == 0.4


def test_daemon_with_no_strategy_fn_passes_none_context(monkeypatch):
    """Backwards-compat: existing callers without strategy_context_fn get
    None passed through → process_match treats it as pure-EV."""
    _silence_delivery(monkeypatch)
    seen = {}

    def fake_process_match(match, window, build_card, *,
                            max_attempts=3, strategy_context=None,
                            strategy_tilt=None):
        seen["context"] = strategy_context
        seen["tilt"] = strategy_tilt
        return {"status": "ok", "match_id": match["match_id"], "window": window,
                "delivered": True, "card": {}}

    monkeypatch.setattr("schedule.runner.process_match", fake_process_match)
    matches = [_match(81002, 7)]
    daemon = SchedulerDaemon(lambda: matches, lambda m: {})  # no strategy fn / tilt
    daemon.tick()
    daemon.pool.shutdown(wait=True)
    assert seen["context"] is None
    assert seen["tilt"] is None


def test_daemon_strategy_context_fn_failure_falls_back_to_none(monkeypatch):
    """If standings_context raises (corrupt DB, schema drift) the daemon must
    NOT crash the tick — process_match runs with context=None (pure-EV)."""
    _silence_delivery(monkeypatch)
    seen = {}

    def fake_process_match(match, window, build_card, *,
                            max_attempts=3, strategy_context=None,
                            strategy_tilt=None):
        seen["context"] = strategy_context
        return {"status": "ok", "match_id": match["match_id"], "window": window,
                "delivered": True, "card": {}}

    monkeypatch.setattr("schedule.runner.process_match", fake_process_match)
    def boom():
        raise RuntimeError("DB corrupted")
    matches = [_match(81003, 7)]
    daemon = SchedulerDaemon(lambda: matches, lambda m: {},
                              strategy_context_fn=boom, strategy_tilt=0.4)
    daemon.tick()
    daemon.pool.shutdown(wait=True)
    assert seen["context"] is None                  # graceful fallback


# ─────────────────── Piece 5: recommend_to_win regression / golden path ───────────────────

def _ev_recommendation(picks: list[dict]) -> dict:
    """A minimal recommendation dict shaped like ev_optimizer.recommend()'s output."""
    return {
        "pick_exact_score": {"home": picks[0]["home"], "away": picks[0]["away"]},
        "pick_direction":  picks[0]["direction"],
        "expected_points": picks[0]["expected_points"],
        "ranked_alternatives": picks,
    }


def test_recommend_to_win_no_op_when_tilt_zero():
    """Tilt=0 must produce the input pick unchanged — the system's default."""
    rec = _ev_recommendation([
        {"home": 2, "away": 0, "direction": "H", "expected_points": 2.0, "p_score": 0.14},
        {"home": 1, "away": 0, "direction": "H", "expected_points": 1.8, "p_score": 0.18},
    ])
    ctx = {"your_points": 30, "leader_points": 50, "second_points": 35, "games_left": 8}
    out = recommend_to_win(rec, ctx, tilt=0)
    assert out["pick_exact_score"] == {"home": 2, "away": 0}
    assert "strategy" not in out                     # no annotation when off


def test_recommend_to_win_no_op_when_context_missing():
    """No standings → no-op (returns pure-EV pick)."""
    rec = _ev_recommendation([
        {"home": 2, "away": 0, "direction": "H", "expected_points": 2.0, "p_score": 0.14},
    ])
    out = recommend_to_win(rec, context=None, tilt=0.5)
    assert out["pick_exact_score"] == {"home": 2, "away": 0}


def test_recommend_to_win_picks_higher_upside_when_behind():
    """When far behind with games left, tilt > 0 chooses the high-upside
    (lower-prob, higher EV-per-prob) candidate from the top-K."""
    rec = _ev_recommendation([
        # Same EV but lower p_score → higher "upside" (= EV / p_score)
        {"home": 1, "away": 0, "direction": "H", "expected_points": 2.0, "p_score": 0.20},
        {"home": 3, "away": 0, "direction": "H", "expected_points": 2.0, "p_score": 0.08},
    ])
    # You're 20 pts behind with 8 games left, swing 6 → capacity 48 → pressure ≈ 0.42
    ctx = {"your_points": 30, "leader_points": 50, "second_points": 35, "games_left": 8}
    out = recommend_to_win(rec, ctx, tilt=0.5)
    # Higher-variance 3-0 wins
    assert out["pick_exact_score"] == {"home": 3, "away": 0}
    assert out["strategy"]["deviated_from_ev"] is True
    assert out["strategy"]["ev_optimal_score"] == {"home": 1, "away": 0}


def test_recommend_to_win_picks_safer_when_ahead():
    """When ahead with games left, tilt > 0 leans toward the safer (higher
    p_score, similar EV) candidate."""
    rec = _ev_recommendation([
        {"home": 3, "away": 0, "direction": "H", "expected_points": 2.0, "p_score": 0.08},
        {"home": 1, "away": 0, "direction": "H", "expected_points": 2.0, "p_score": 0.20},
    ])
    # You're 15 pts ahead of second with 8 games left
    ctx = {"your_points": 60, "leader_points": 60, "second_points": 45, "games_left": 8}
    out = recommend_to_win(rec, ctx, tilt=0.5)
    assert out["pick_exact_score"] == {"home": 1, "away": 0}    # safer
    assert out["strategy"]["deviated_from_ev"] is True


def test_recommend_to_win_neutral_when_tied():
    """Tied with leader, no pressure → pure-EV pick."""
    rec = _ev_recommendation([
        {"home": 2, "away": 0, "direction": "H", "expected_points": 2.0, "p_score": 0.14},
        {"home": 3, "away": 0, "direction": "H", "expected_points": 1.95, "p_score": 0.08},
    ])
    ctx = {"your_points": 40, "leader_points": 40, "second_points": 40, "games_left": 8}
    out = recommend_to_win(rec, ctx, tilt=0.5)
    assert out["pick_exact_score"] == {"home": 2, "away": 0}    # EV-optimal wins


def test_recommend_to_win_zero_games_left_no_op():
    """End of tournament → pressure clamps to 0 → pure-EV pick."""
    rec = _ev_recommendation([
        {"home": 1, "away": 0, "direction": "H", "expected_points": 2.0, "p_score": 0.20},
        {"home": 3, "away": 0, "direction": "H", "expected_points": 2.0, "p_score": 0.08},
    ])
    ctx = {"your_points": 30, "leader_points": 50, "second_points": 35, "games_left": 0}
    out = recommend_to_win(rec, ctx, tilt=0.5)
    assert out["pick_exact_score"] == {"home": 1, "away": 0}    # first candidate (EV pick)


# ─────────────────── Math sanity-check ───────────────────

def test_risk_pressure_formula_behind():
    """Reference value: you 30, leader 50, 8 games × 6 swing = 48 capacity.
    Pressure = min(1, (50-30)/48) ≈ 0.417"""
    p = risk_pressure(30, 50, 8, second_points=35)
    assert 0.41 <= p <= 0.42


def test_risk_pressure_formula_ahead():
    """You 60, second 50, capacity 48. Pressure = -min(1, (60-50)/48) ≈ -0.208"""
    p = risk_pressure(60, 60, 8, second_points=50)
    assert -0.22 <= p <= -0.20


def test_risk_pressure_clamps_to_one_when_huge_gap():
    """Gap larger than total swing capacity → clamps to +/- 1."""
    assert risk_pressure(0, 100, 4, 0) == 1.0                # behind by 100, capacity 24
    assert risk_pressure(100, 100, 4, 0) == -1.0             # ahead by 100, capacity 24


def test_risk_pressure_zero_games_left():
    """No games left → no capacity → pressure 0 regardless of standings."""
    assert risk_pressure(30, 50, 0, 35) == 0.0
    assert risk_pressure(60, 60, 0, 50) == 0.0

"""Strategy layer wired into the pipeline: connected, default-off, deviates when
enabled, and fallback-safe."""
import sqlite3
from orchestrator.pipeline import process_match
from core.decision.strategy import recommend_to_win
from store import repo

RANKED = [
    {"home": 1, "away": 0, "direction": "H", "p_score": 0.30, "expected_points": 2.0},
    {"home": 2, "away": 1, "direction": "H", "p_score": 0.10, "expected_points": 1.8},
    {"home": 3, "away": 2, "direction": "H", "p_score": 0.03, "expected_points": 1.5},
]


def _card():
    return {"home": "A", "away": "B", "stage": "Group",
            "pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
            "expected_points": 2.0, "model_prob": {"H": .5, "D": .3, "A": .2},
            "locked_odds": {"H": 2.0, "D": 3.0, "A": 4.0},
            "ranked_alternatives": RANKED}


def _patch(monkeypatch):
    import core.delivery as d
    sent = {}
    monkeypatch.setattr(d, "deliver_card", lambda c: sent.update(card=c) or True)
    monkeypatch.setattr(d, "alert", lambda t, b: True)
    return sent


def test_pipeline_default_is_pure_ev(monkeypatch):
    sent = _patch(monkeypatch)
    res = process_match({"match_id": 1, "home": "A", "away": "B"}, "T-7m",
                        build_card=lambda m: _card())            # no context, no tilt
    assert res["status"] == "ok"
    assert sent["card"]["pick_exact_score"] == {"home": 1, "away": 0}   # EV pick, untouched
    assert "strategy" not in sent["card"]


def test_pipeline_with_context_and_tilt_deviates_when_behind(monkeypatch):
    sent = _patch(monkeypatch)
    ctx = {"your_points": 0, "leader_points": 50, "games_left": 1}
    res = process_match({"match_id": 2, "home": "A", "away": "B"}, "T-7m",
                        build_card=lambda m: _card(),
                        strategy_context=ctx, strategy_tilt=0.5)
    assert sent["card"]["pick_exact_score"] == {"home": 3, "away": 2}   # took variance
    assert sent["card"]["strategy"]["deviated_from_ev"] is True


def test_strategy_is_fallback_safe_on_bad_input():
    # missing ranked_alternatives / odd context must not raise; returns EV pick
    rec = {"pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
           "expected_points": 2.0}
    out = recommend_to_win(rec, {"your_points": 0, "leader_points": 9, "games_left": 1}, 0.5)
    assert out["pick_exact_score"] == {"home": 1, "away": 0}


# --- standings_context provider from the store ---
def _db():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE matches (match_id INTEGER PRIMARY KEY, status TEXT);
      CREATE TABLE standings (participant TEXT PRIMARY KEY, group_points REAL,
        knockout_points REAL, futures_points REAL,
        side_points REAL DEFAULT 0);
    """)
    return conn


def test_standings_context_none_when_empty():
    assert repo.standings_context(_db()) is None      # safe no-op


def test_standings_context_built():
    """Day-9.5 fix: standings_context now sums columns raw — the §14 -15 %
    reset is the writer's responsibility (core/scoring/standings_writer),
    not the reader's. So values entered via tools/standings_set.py flow
    through untouched. This test enters the POST-reset values directly
    (matching what the Negev app displays after the group→KO transition)."""
    conn = _db()
    conn.execute("INSERT INTO matches VALUES (1,'FINISHED')")
    conn.execute("INSERT INTO matches VALUES (2,'TIMED')")
    # Day-9.27: standings table gained a side_points column. Use explicit
    # column list to avoid breaking when columns are added.
    conn.execute("INSERT INTO standings (participant, group_points, "
                  "knockout_points, futures_points, side_points) "
                  "VALUES ('Igor', 85, 0, 0, 0)")
    conn.execute("INSERT INTO standings (participant, group_points, "
                  "knockout_points, futures_points, side_points) "
                  "VALUES ('Dana', 170, 0, 0, 0)")
    conn.commit()
    ctx = repo.standings_context(conn, me="Igor")
    assert ctx["games_left"] == 1
    assert ctx["leader_points"] == 170.0 and ctx["your_points"] == 85.0

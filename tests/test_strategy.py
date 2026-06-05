"""Win-probability strategy layer: variance when behind, protect when ahead,
pure-EV when neutral / tilt=0."""
from core.decision.strategy import risk_pressure, strategize, recommend_to_win

# top-EV first; later candidates have higher upside (EV/p_score)
RANKED = [
    {"home": 1, "away": 0, "direction": "H", "p_score": 0.30, "expected_points": 2.0},   # safe
    {"home": 2, "away": 1, "direction": "H", "p_score": 0.10, "expected_points": 1.8},   # mid upside
    {"home": 3, "away": 2, "direction": "H", "p_score": 0.03, "expected_points": 1.5},   # high upside
]


def test_pressure_behind_positive():
    assert risk_pressure(your_points=0, leader_points=50, games_left=1) > 0


def test_pressure_ahead_negative():
    assert risk_pressure(your_points=50, leader_points=50, games_left=1, second_points=40) < 0


def test_pressure_neutral_when_no_games_left():
    assert risk_pressure(0, 50, 0) == 0.0


def test_behind_takes_variance():
    ctx = {"your_points": 0, "leader_points": 50, "games_left": 1}  # big gap, last game
    pick = strategize(RANKED, ctx, tilt=0.5)
    assert (pick["home"], pick["away"]) == (3, 2)        # the highest-upside near-EV pick


def test_ahead_protects():
    ctx = {"your_points": 50, "leader_points": 50, "games_left": 1, "second_points": 40}
    pick = strategize(RANKED, ctx, tilt=0.5)
    assert (pick["home"], pick["away"]) == (1, 0)        # the safest pick


def test_tilt_zero_is_pure_ev():
    ctx = {"your_points": 0, "leader_points": 50, "games_left": 1}
    assert strategize(RANKED, ctx, tilt=0.0) is RANKED[0]


def test_no_context_is_pure_ev():
    assert strategize(RANKED, None, tilt=0.5) is RANKED[0]


def test_recommend_to_win_wraps_and_notes():
    rec = {"pick_exact_score": {"home": 1, "away": 0}, "pick_direction": "H",
           "expected_points": 2.0, "ranked_alternatives": RANKED}
    out = recommend_to_win(rec, {"your_points": 0, "leader_points": 50, "games_left": 1},
                           tilt=0.5)
    assert out["pick_exact_score"] == {"home": 3, "away": 2}
    assert out["strategy"]["deviated_from_ev"] is True
    assert out["strategy"]["ev_optimal_score"] == {"home": 1, "away": 0}

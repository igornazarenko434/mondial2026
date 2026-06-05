"""Model->decision assembler + Day-3 calibrate flow."""
import pytest
from core.models.predict import score_distribution, match_card

ELO = {"France": 2050.0, "Norway": 1840.0}
EG = lambda h, a: (0.9, 1.8) if h == "Norway" else (1.5, 1.0)   # stand-in fitted fn


def test_score_distribution_normalised_and_favours_stronger():
    m = score_distribution("Norway", "France", EG, ELO, {"H": 4.2, "D": 3.6, "A": 1.85})
    assert abs(m.sum() - 1) < 1e-9


def test_match_card_full_shape():
    c = match_card("Norway", "France", "Group", True, EG, ELO, {"H": 4.2, "D": 3.6, "A": 1.85})
    for k in ("pick_exact_score", "pick_direction", "expected_points", "model_prob",
              "home", "away", "stage", "detonator"):
        assert k in c
    assert c["detonator"] is True and c["expected_points"] is not None


def test_news_deltas_shift_the_distribution():
    base = score_distribution("Norway", "France", EG, ELO, {"H": 4.2, "D": 3.6, "A": 1.85})
    adj = score_distribution("Norway", "France", EG, ELO, {"H": 4.2, "D": 3.6, "A": 1.85},
                             news_deltas=(-0.3, 0.15))
    from core.scoring.engine import direction_probs
    assert direction_probs(adj)["A"] >= direction_probs(base)["A"]   # France up


def test_degrades_to_modal_pick_without_odds():
    c = match_card("Norway", "France", "Group", False, EG, ELO, scoring_odds=None)
    assert c["expected_points"] is None and "note" in c
    assert c["pick_exact_score"] == c["modal_score"]          # falls back to most-likely


def test_bad_odds_degrade_not_crash():
    c = match_card("Norway", "France", "Group", False, EG, ELO, {"H": 0, "D": 0, "A": 0})
    assert c["pick_exact_score"] == c["modal_score"]          # invalid odds -> modal


def test_unknown_team_uses_fallback_elo_baseline():
    c = match_card("Atlantis", "France", "Group", False, lambda h, a: (1.3, 1.1),
                   {}, {"H": 2.5, "D": 3.2, "A": 2.7})
    assert set(c["pick_exact_score"]) == {"home", "away"}     # still produces a pick


# ---- Day-3 calibrate flow (no scipy needed: inject a trivial fit via results) ----
def test_calibrate_run(monkeypatch):
    pytest.importorskip("scipy"); pytest.importorskip("pandas")
    from tools import calibrate
    results = lambda: [{"home": "A", "away": "B", "home_goals": 2, "away_goals": 0,
                        "days_ago": 10} for _ in range(20)]
    samples = [{"dc": {"H": .6, "D": .25, "A": .15}, "elo": {"H": .55, "D": .25, "A": .2},
                "market": {"H": .6, "D": .25, "A": .15}, "actual": "H"} for _ in range(30)]
    out = calibrate.run(results, samples)
    assert out["n_results"] == 20 and out["fitted_teams"] == 2
    assert abs(sum(out["recommended_weights"].values()) - 1) < 1e-6
    assert "log-loss" in calibrate.report(out)

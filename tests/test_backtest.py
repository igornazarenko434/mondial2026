"""Day-3 backtest + tuning + results loader + (optional) DC fit."""
import math
import pytest
from core.models import backtest as bt
from core.data.results_io import historical_results


# ---------------- scoring rules ----------------
def test_log_loss_and_brier_known_values():
    p = {"H": 1.0, "D": 0.0, "A": 0.0}
    assert bt.log_loss(p, "H") < 1e-6                  # perfect prediction → ~0
    assert bt.log_loss(p, "A") > 20                    # confidently wrong → huge
    assert bt.brier({"H": 1, "D": 0, "A": 0}, "H") == 0.0


def test_blend_normalised():
    b = bt.blend({"H": .5, "D": .3, "A": .2}, {"H": .4, "D": .3, "A": .3},
                 {"H": .6, "D": .25, "A": .15}, {"dixon_coles": .3, "elo": .2, "market": .5})
    assert abs(sum(b.values()) - 1) < 1e-9


# ---------------- tuning picks the predictive source ----------------
def _samples_market_is_truth(n=40):
    # market always puts 0.8 on the actual outcome; dc/elo are uninformative.
    out = []
    for k in range(n):
        actual = ("H", "D", "A")[k % 3]
        market = {o: (0.8 if o == actual else 0.1) for o in ("H", "D", "A")}
        flat = {"H": 1/3, "D": 1/3, "A": 1/3}
        out.append({"dc": flat, "elo": flat, "market": market, "actual": actual})
    return out


def test_tuning_favours_the_accurate_source():
    res = bt.tune_blend_weights(_samples_market_is_truth())
    assert res["best_weights"]["market"] >= 0.6        # leans on the source that predicts
    assert res["beats_market"] in (True, False)        # comparison computed
    assert res["best_metrics"]["log_loss"] <= res["market_baseline"]["log_loss"] + 1e-9


def test_evaluate_and_calibration_shapes():
    s = _samples_market_is_truth(30)
    m = bt.evaluate(s, {"market": 1.0})
    assert m["n"] == 30 and m["log_loss"] > 0
    cal = bt.calibration(s, {"market": 1.0})
    assert all("avg_pred" in b and "obs_rate" in b for b in cal)


# ---------------- results loader (DI + normalization) ----------------
def test_results_loader_normalizes_and_computes_days():
    rows = [{"home": "Korea Republic", "away": "Cabo Verde", "home_goals": 2,
             "away_goals": 1, "date": "2025-09-01"},
            {"home": "X", "away": "Y", "home_goals": None, "away_goals": 1},  # dropped
            {"home": "Türkiye", "away": "DR Congo", "home_goals": 0, "away_goals": 0,
             "days_ago": 10}]
    out = historical_results(fetch=lambda: rows)
    assert len(out) == 2                                # null-score row dropped
    assert out[0]["home"] == "South Korea" and out[0]["away"] == "Cape Verde"
    assert out[0]["days_ago"] >= 0
    assert out[1]["home"] == "Türkiye" and out[1]["days_ago"] == 10


# ---------------- DC fit (needs scipy+pandas; skipped if absent) ----------------
def test_dixon_coles_fit_on_synthetic():
    pytest.importorskip("scipy"); pytest.importorskip("pandas")
    from core.models.fit import fit_from_results, expected_goals_fn
    # synthetic: A strong (scores lots), B weak
    res = []
    for _ in range(12):
        res += [{"home": "A", "away": "B", "home_goals": 3, "away_goals": 0, "days_ago": 30},
                {"home": "B", "away": "A", "home_goals": 0, "away_goals": 2, "days_ago": 30}]
    strengths = fit_from_results(res)
    eg = expected_goals_fn(strengths)
    h, a = eg("A", "B")
    assert h > a                                        # strong A expected to outscore B
    assert eg("Nowhere", "Elsewhere") == (1.3, 1.1)     # fallback for unknown teams


def test_fit_stable_on_high_scores_no_overflow():
    """Regression: high goal counts used to overflow lam**k/factorial. The fit must
    now handle blowout-heavy histories via the log-space Poisson."""
    pytest.importorskip("scipy"); pytest.importorskip("pandas")
    from core.models.fit import fit_from_results, expected_goals_fn
    res = [{"home": "A", "away": "B", "home_goals": g, "away_goals": max(0, g - 2),
            "days_ago": 20} for g in (5, 6, 7, 8, 9) for _ in range(8)]
    eg = expected_goals_fn(fit_from_results(res))       # must not raise
    h, a = eg("A", "B")
    assert h > 0 and a > 0 and h > a

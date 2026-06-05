"""EV optimizer sanity checks."""
import numpy as np
from core.models.dixon_coles import score_matrix
from core.decision.ev_optimizer import rank_scores, recommend

ODDS = {"H": 1.85, "D": 3.60, "A": 4.20}


def test_rank_returns_sorted_ev():
    m = score_matrix(1.6, 1.1)
    ranked = rank_scores(m, "Group", ODDS, top=5)
    evs = [r["expected_points"] for r in ranked]
    assert evs == sorted(evs, reverse=True)
    assert len(ranked) == 5


def test_detonator_doubles_ev():
    m = score_matrix(1.6, 1.1)
    base = rank_scores(m, "Group", ODDS, detonator=False, top=1)[0]["expected_points"]
    det = rank_scores(m, "Group", ODDS, detonator=True, top=1)[0]["expected_points"]
    assert abs(det - 2 * base) < 5e-3   # outputs are rounded to 3 decimals


def test_recommend_shape():
    m = score_matrix(1.6, 1.1)
    rec = recommend(m, "Group", ODDS)
    for key in ("pick_exact_score", "pick_direction", "expected_points",
                "modal_score", "model_prob"):
        assert key in rec
    probs = rec["model_prob"]
    assert abs(probs["H"] + probs["D"] + probs["A"] - 1.0) < 1e-6


def test_matrix_normalised():
    m = score_matrix(2.0, 0.8)
    assert abs(m.sum() - 1.0) < 1e-9

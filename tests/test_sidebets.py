"""Daily side-bet recommender."""
import numpy as np
from core.models.dixon_coles import score_matrix
from core.decision.sidebets import (total_goals_pmf, combined_total_pmf,
                                     recommend_total_goals, recommend_yes_no)


def test_total_goals_pmf_sums_to_one():
    m = score_matrix(1.5, 1.2)
    assert abs(total_goals_pmf(m).sum() - 1.0) < 1e-9


def test_combined_pmf_normalised():
    ms = [score_matrix(1.4, 1.1), score_matrix(0.9, 0.8), score_matrix(2.0, 1.3)]
    assert abs(combined_total_pmf(ms).sum() - 1.0) < 1e-9


def test_recommend_total_goals_consistent():
    ms = [score_matrix(1.4, 1.1), score_matrix(0.9, 0.8)]  # ~ low-scoring two games
    low = recommend_total_goals(ms, line=8.5)
    assert low["recommend"] == "under"                     # 2 games rarely > 8.5
    assert abs(low["p_over"] + low["p_under"] - 1.0) < 1e-9
    high = recommend_total_goals(ms, line=1.5)
    assert high["recommend"] == "over"                     # almost surely > 1.5


def test_recommend_yes_no():
    assert recommend_yes_no(0.7, "BTTS")["recommend"] == "yes"
    assert recommend_yes_no(0.3)["recommend"] == "no"

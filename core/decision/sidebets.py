"""Daily side-bet recommender (§17).

Each match day the group publishes a yes/no side bet (e.g. "over 8.5 total goals
across today's games?"). These are scored on correctness (1.0 group / 1.5
R32–QF / 2.0 SF·final) — no odds multiplier — so the best-practice pick is simply
**the more probable side**, computed from the per-match score matrices we already
build. No position/standings logic here (that's the opt-in strategy layer).
"""
from __future__ import annotations
import numpy as np


def total_goals_pmf(matrix: np.ndarray) -> np.ndarray:
    """P(total goals = t) for one match, from its score matrix."""
    n, m = matrix.shape
    pmf = np.zeros(n + m - 1)
    for i in range(n):
        for j in range(m):
            pmf[i + j] += matrix[i, j]
    return pmf


def combined_total_pmf(matrices: list[np.ndarray]) -> np.ndarray:
    """P(total goals across several matches) — convolution of per-match pmfs."""
    pmf = np.array([1.0])
    for mtx in matrices:
        pmf = np.convolve(pmf, total_goals_pmf(mtx))
    return pmf / pmf.sum()


def recommend_total_goals(matrices: list[np.ndarray], line: float) -> dict:
    """Recommend over/under for a 'total goals today' side bet.

    line: e.g. 8.5 (use .5 lines to avoid pushes). For an integer line, 'over'
    means strictly greater.
    """
    pmf = combined_total_pmf(matrices)
    p_over = float(sum(p for t, p in enumerate(pmf) if t > line))
    p_under = 1.0 - p_over
    return {"market": f"total goals over/under {line}",
            "p_over": round(p_over, 3), "p_under": round(p_under, 3),
            "recommend": "over" if p_over >= p_under else "under",
            "confidence": round(abs(p_over - p_under), 3)}


def recommend_yes_no(prob_yes: float, label: str = "side bet") -> dict:
    """Generic yes/no side bet: pick the more probable side. Use for any event
    whose probability you can compute from the models (e.g. 'a red card today',
    'both teams score in match X')."""
    prob_yes = max(0.0, min(1.0, prob_yes))
    return {"market": label, "p_yes": round(prob_yes, 3),
            "p_no": round(1 - prob_yes, 3),
            "recommend": "yes" if prob_yes >= 0.5 else "no",
            "confidence": round(abs(prob_yes - 0.5) * 2, 3)}

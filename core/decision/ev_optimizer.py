"""Expected-points optimizer — the system's edge.

Given a score-probability matrix and the locked odds, it picks the scoreline
that MAXIMIZES expected points under the friends' rules (not the most-likely
score). Derivation for predicting score s with direction d:

    EV(s) = odds(d) * detonator * [ base * (P(d) - P(s)) + tableMult(s) * P(s) ]

i.e. you collect base*odds for any correct-direction result, upgraded to
tableMult*odds on the exact scoreline.
"""
from __future__ import annotations
import numpy as np
from config.rules import STAGE_TYPE, BASE_POINTS, DETONATOR_FACTOR
from core.scoring.engine import direction, exact_multiplier, direction_probs


def rank_scores(matrix: np.ndarray, stage: str, odds: dict,
                detonator: bool = False, top: int = 5) -> list[dict]:
    """Return candidate scorelines ranked by expected points (descending)."""
    stype = STAGE_TYPE.get(stage)
    if stype is None:
        raise ValueError(f"unknown stage '{stage}'; valid: {sorted(STAGE_TYPE)}")
    base = BASE_POINTS[stype]
    det = DETONATOR_FACTOR if detonator else 1.0
    pdir = direction_probs(matrix)
    n = matrix.shape[0]

    rows = []
    for i in range(n):
        for j in range(n):
            d = direction(i, j)
            p_s = matrix[i, j]
            w, l = max(i, j), min(i, j)
            mult = exact_multiplier(stype, w, l)
            ev = odds[d] * det * (base * (pdir[d] - p_s) + mult * p_s)
            rows.append({"home": i, "away": j, "direction": d,
                         "p_score": round(float(p_s), 4),
                         "expected_points": round(float(ev), 3)})
    rows.sort(key=lambda r: r["expected_points"], reverse=True)
    return rows[:top]


def recommend(matrix: np.ndarray, stage: str, odds: dict,
              detonator: bool = False) -> dict:
    """Full recommendation: EV-optimal score, modal score, direction probs."""
    ranked = rank_scores(matrix, stage, odds, detonator, top=5)
    best = ranked[0]
    # most-likely (modal) score, for transparency
    idx = np.unravel_index(np.argmax(matrix), matrix.shape)
    pdir = direction_probs(matrix)
    return {
        "pick_exact_score": {"home": best["home"], "away": best["away"]},
        "pick_direction": best["direction"],
        "expected_points": best["expected_points"],
        "modal_score": {"home": int(idx[0]), "away": int(idx[1])},
        "model_prob": {k: round(v, 3) for k, v in pdir.items()},
        "ranked_alternatives": ranked,
        "detonator": detonator,
        "locked_odds": odds,
    }
